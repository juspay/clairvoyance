/* Maths for the generated DPDFNet program (build.py writes the program; runtime.c runs it).
 *
 * Every size, stride and offset arrives as an argument baked in at build time from the model's
 * fixed shapes, so nothing here depends on the audio. No allocation, float32 throughout, and no
 * fast-math: loops are written so the compiler can vectorise them without reordering sums. */
#include <math.h>
#include <stdint.h>
#include <string.h>

#define MAXR 8

/* dst[box] = src[box] with any strides (in elements): copies, slices, transposes, concats. */
static void copy_nd(float *dst, const long *ds, const float *src, const long *ss, int R, const int *shape) {
    int idx[MAXR] = {0};
    long outer = 1;
    for (int i = 0; i < R - 1; i++) outer *= shape[i];
    const int n = shape[R - 1];
    const long dl = ds[R - 1], sl = ss[R - 1];
    for (long it = 0; it < outer; it++) {
        long doff = 0, soff = 0;
        for (int i = 0; i < R - 1; i++) { doff += idx[i] * ds[i]; soff += idx[i] * ss[i]; }
        float *d = dst + doff;
        const float *s = src + soff;
        if (dl == 1 && sl == 1) memcpy(d, s, (size_t)n * sizeof(float));
        else for (int j = 0; j < n; j++) d[j * dl] = s[j * sl];
        for (int i = R - 2; i >= 0; i--) { if (++idx[i] < shape[i]) break; idx[i] = 0; }
    }
}

/* Elementwise binary op with broadcasting. o is contiguous with shape os; the input strides are 0
 * on broadcast axes. Fast paths for the common last-axis layouts. */
#define DEF_EW(NAME, EXPR)                                                                              \
    static void NAME(float *o, const float *a, const float *b, int R, const int *os, const long *as,     \
                     const long *bs) {                                                                 \
        int idx[MAXR] = {0};                                                                           \
        long outer = 1;                                                                                \
        for (int i = 0; i < R - 1; i++) outer *= os[i];                                                \
        const int n = os[R - 1];                                                                       \
        const long sa = as[R - 1], sb = bs[R - 1];                                                     \
        for (long it = 0; it < outer; it++) {                                                          \
            long ao = 0, bo = 0;                                                                       \
            for (int i = 0; i < R - 1; i++) { ao += idx[i] * as[i]; bo += idx[i] * bs[i]; }            \
            const float *pa = a + ao, *pb = b + bo;                                                    \
            if (sa == 1 && sb == 1) {                                                                  \
                for (int j = 0; j < n; j++) { const float x = pa[j], y = pb[j]; (void)y; o[j] = (EXPR); } \
            } else if (sa == 1 && sb == 0) {                                                           \
                const float y = pb[0]; (void)y;                                                        \
                for (int j = 0; j < n; j++) { const float x = pa[j]; o[j] = (EXPR); }                  \
            } else if (sa == 0 && sb == 1) {                                                           \
                const float x = pa[0];                                                                 \
                for (int j = 0; j < n; j++) { const float y = pb[j]; (void)y; o[j] = (EXPR); }         \
            } else {                                                                                   \
                for (int j = 0; j < n; j++) {                                                          \
                    const float x = pa[j * sa], y = pb[j * sb]; (void)y; o[j] = (EXPR);                 \
                }                                                                                      \
            }                                                                                          \
            o += n;                                                                                    \
            for (int i = R - 2; i >= 0; i--) { if (++idx[i] < os[i]) break; idx[i] = 0; }             \
        }                                                                                              \
    }
DEF_EW(ew_add, x + y)
DEF_EW(ew_sub, x - y)
DEF_EW(ew_mul, x * y)
DEF_EW(ew_div, x / y)
DEF_EW(ew_pow, powf(x, y))
DEF_EW(ew_sq, x * x) /* Pow with a constant exponent of 2 */

/* exp(x) with Cephes' polynomial (about 1 ulp). Branch-free, so loops over it vectorise. The clamp
 * keeps the exponent bits in the normal range */
static inline float fexp(float x) {
    x = x < -87.0f ? -87.0f : x;
    x = x > 88.0f ? 88.0f : x;
    const float n = rintf(x * 1.44269504088896341f);
    const float r = x - n * 0.693359375f + n * 2.12194440e-4f;
    float p = 1.9875691500e-4f;
    p = p * r + 1.3981999507e-3f;
    p = p * r + 8.3334519073e-3f;
    p = p * r + 4.1665795894e-2f;
    p = p * r + 1.6666665459e-1f;
    p = p * r + 5.0000001201e-1f;
    const float y = p * r * r + r + 1.0f;
    union { int32_t i; float f; } u;
    u.i = ((int32_t)n + 127) << 23;
    return y * u.f;
}
static inline float sigm(float x) { return 1.0f / (1.0f + fexp(-x)); }
static inline float ftanh(float x) { return 2.0f / (1.0f + fexp(-2.0f * x)) - 1.0f; }

static void u_relu(float *o, const float *a, long n) { for (long i = 0; i < n; i++) o[i] = a[i] > 0.0f ? a[i] : 0.0f; }
static void u_sigmoid(float *o, const float *a, long n) { for (long i = 0; i < n; i++) o[i] = sigm(a[i]); }
static void u_tanh(float *o, const float *a, long n) { for (long i = 0; i < n; i++) o[i] = ftanh(a[i]); }
static void u_sqrt(float *o, const float *a, long n) { for (long i = 0; i < n; i++) o[i] = sqrtf(a[i]); }
static void u_log(float *o, const float *a, long n) { for (long i = 0; i < n; i++) o[i] = logf(a[i]); }

/* ReduceSum with keepdims=0: ostr is the output stride of each input axis (0 on reduced axes). */
static void reduce_sum(float *o, long on, const float *a, int R, const int *shape, const long *ostr) {
    memset(o, 0, (size_t)on * sizeof(float));
    int idx[MAXR] = {0};
    long total = 1;
    for (int i = 0; i < R; i++) total *= shape[i];
    for (long t = 0; t < total; t++) {
        long oo = 0;
        for (int i = 0; i < R; i++) oo += idx[i] * ostr[i];
        o[oo] += a[t];
        for (int i = R - 1; i >= 0; i--) { if (++idx[i] < shape[i]) break; idx[i] = 0; }
    }
}

/* LayerNormalization over the last axis, with ONNX Runtime's formula. */
static void layernorm(float *o, const float *x, const float *g, const float *b, long rows, int D, float eps) {
    for (long r = 0; r < rows; r++) {
        const float *p = x + r * D;
        float *q = o + r * D;
        float mean = 0.0f, ms = 0.0f;
        for (int i = 0; i < D; i++) { mean += p[i]; ms += p[i] * p[i]; }
        mean /= (float)D;
        ms = sqrtf(ms / (float)D - mean * mean + eps);
        for (int i = 0; i < D; i++) q[i] = (p[i] - mean) / ms * g[i] + (b ? b[i] : 0.0f);
    }
}

/* One output row, W columns at a time: Y[n0:n0+W] = a[K] x Bt[K, n0:n0+W] (+ bias). The W partial
 * sums stay in registers while Bt streams through once. */
#define DEF_ROW(W)                                                                                      \
    static inline void row##W(float *restrict y, const float *restrict a, const float *restrict Bt,       \
                              const float *restrict bias, int K, int N, int n0) {                       \
        float acc[W];                                                                                   \
        for (int j = 0; j < W; j++) acc[j] = bias ? bias[n0 + j] : 0.0f;                                \
        for (int k = 0; k < K; k++) {                                                                   \
            const float *restrict b = Bt + (long)k * N + n0;                                            \
            const float av = a[k];                                                                      \
            for (int j = 0; j < W; j++) acc[j] += av * b[j];                                            \
        }                                                                                               \
        for (int j = 0; j < W; j++) y[n0 + j] = acc[j];                                                 \
    }
DEF_ROW(64)
DEF_ROW(16)

/* Y[M,N] = A[M,K] x Bt[K,N] + bias[N], with Bt stored K-major (build.py pre-transposes weights).
 * Row by row: a weight matrix (at most 768 KB here) stays in cache across rows. */
static void gemm_kt(float *restrict Y, const float *restrict A, const float *restrict Bt, const float *restrict bias,
                    int M, int K, int N) {
    for (int m = 0; m < M; m++) {
        float *restrict y = Y + (long)m * N;
        const float *restrict a = A + (long)m * K;
        int n = 0;
        for (; n + 64 <= N; n += 64) row64(y, a, Bt, bias, K, N, n);
        for (; n + 16 <= N; n += 16) row16(y, a, Bt, bias, K, N, n);
        for (; n < N; n++) {
            float s = bias ? bias[n] : 0.0f;
            for (int k = 0; k < K; k++) s += a[k] * Bt[(long)k * N + n];
            y[n] = s;
        }
    }
}

static void add_rowbias(float *Y, const float *b, int M, int N) {
    for (int m = 0; m < M; m++)
        for (int n = 0; n < N; n++) Y[(long)m * N + n] += b[m];
}

/* Conv2D, batch 1, NCHW, with groups, strides, dilations and pads (top, left, bottom, right). Loops
 * over kernel taps and accumulates shifted input rows, so the inner loop is contiguous at stride 1. */
static void conv2d(float *restrict y, const float *restrict x, const float *restrict w, const float *restrict bias,
                   int C, int H, int W, int M, int KH, int KW, int OH, int OW, int SH, int SW, int PT, int PL,
                   int DH, int DW, int G) {
    const int Cg = C / G, Mg = M / G;
    for (int m = 0; m < M; m++) {
        const float bv = bias ? bias[m] : 0.0f;
        for (int i = 0; i < OH * OW; i++) y[(long)m * OH * OW + i] = bv;
    }
    for (int m = 0; m < M; m++) {
        const int g = m / Mg;
        float *restrict ym = y + (long)m * OH * OW;
        for (int cc = 0; cc < Cg; cc++) {
            const float *restrict xc = x + (long)(g * Cg + cc) * H * W;
            for (int ki = 0; ki < KH; ki++)
                for (int kj = 0; kj < KW; kj++) {
                    const float wv = w[(((long)m * Cg + cc) * KH + ki) * KW + kj];
                    const int off_w = kj * DW - PL;
                    int ow0 = 0, ow1 = OW; /* outputs whose input column is inside the row */
                    while (ow0 < OW && ow0 * SW + off_w < 0) ow0++;
                    while (ow1 > ow0 && (ow1 - 1) * SW + off_w >= W) ow1--;
                    for (int oh = 0; oh < OH; oh++) {
                        const int ih = oh * SH - PT + ki * DH;
                        if (ih < 0 || ih >= H) continue;
                        float *restrict yr = ym + (long)oh * OW;
                        const float *restrict xr = xc + (long)ih * W + off_w;
                        if (SW == 1)
                            for (int ow = ow0; ow < ow1; ow++) yr[ow] += wv * xr[ow];
                        else
                            for (int ow = ow0; ow < ow1; ow++) yr[ow] += wv * xr[ow * SW];
                    }
                }
        }
    }
}

/* ONNX GRU: bidirectional, batch 1, linear_before_reset=1, gates z|r|h, default activations.
 * Weights pre-transposed by build.py: Wt[2][I][3H], Rt[2][H][3H]; B[2][6H] = Wb|Rb; h0[2][H] or NULL.
 * X[T][I] -> Y[T][2][H], Yh[2][H]. tmp holds T*3H + 3H + 2H floats. */
static void gru_bidir(float *restrict Y, float *restrict Yh, const float *restrict X, const float *restrict Wt,
                      const float *restrict Rt, const float *restrict Bt, const float *restrict h0, int T, int I,
                      int H, float *restrict tmp) {
    const int G = 3 * H;
    float *xw = tmp, *hr = tmp + (long)T * G, *h = hr + G, *hn = h + H;
    for (int d = 0; d < 2; d++) {
        const float *W = Wt + (long)d * I * G, *R = Rt + (long)d * H * G, *Wb = Bt + (long)d * 2 * G, *Rb = Wb + G;
        gemm_kt(xw, X, W, Wb, T, I, G); /* input projections for every step at once */
        for (int k = 0; k < H; k++) h[k] = h0 ? h0[d * H + k] : 0.0f;
        for (int s = 0; s < T; s++) {
            const int t = d == 0 ? s : T - 1 - s;
            gemm_kt(hr, h, R, Rb, 1, H, G);
            const float *xt = xw + (long)t * G;
            for (int k = 0; k < H; k++) {
                const float z = sigm(xt[k] + hr[k]);
                const float r = sigm(xt[H + k] + hr[H + k]);
                const float nn = ftanh(xt[2 * H + k] + r * hr[2 * H + k]);
                hn[k] = (1.0f - z) * nn + z * h[k];
            }
            for (int k = 0; k < H; k++) { h[k] = hn[k]; Y[((long)t * 2 + d) * H + k] = hn[k]; }
        }
        for (int k = 0; k < H; k++) Yh[d * H + k] = h[k];
    }
}

/* ONNX "reflect" padding, any rank; build.py checks every pad is smaller than its axis. */
static void pad_reflect(float *o, const float *a, int R, const int *ishape, const int *oshape, const int *begin) {
    int idx[MAXR] = {0};
    long total = 1, istr[MAXR];
    for (int i = 0; i < R; i++) total *= oshape[i];
    istr[R - 1] = 1;
    for (int i = R - 2; i >= 0; i--) istr[i] = istr[i + 1] * ishape[i + 1];
    for (long t = 0; t < total; t++) {
        long off = 0;
        for (int i = 0; i < R; i++) {
            int c = idx[i] - begin[i];
            const int n = ishape[i];
            if (c < 0) c = -c;
            if (c >= n) c = 2 * (n - 1) - c;
            off += c * istr[i];
        }
        o[t] = a[off];
        for (int i = R - 1; i >= 0; i--) { if (++idx[i] < oshape[i]) break; idx[i] = 0; }
    }
}
