/* Noizzen engine: runs one generated DPDFNet program.
 *
 * The weights file is memory-mapped read-only and shared by every call in the process, never
 * copied. Each call (stream) gets one scratch area and two state buffers, allocated once when it
 * opens; processing a frame allocates nothing. Built by build.py with NOIZZEN_GEN naming the
 * generated program for one model, so the library only accepts that model's weights file. */
#define _POSIX_C_SOURCE 200809L
#include <fcntl.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#if defined(__x86_64__) || defined(__i386__)
#include <xmmintrin.h>
#endif

#include "kernels.h"
#include NOIZZEN_GEN

#define NOIZZEN_API __attribute__((visibility("default")))
#define NOIZZEN_ABI 2
#define HEADER 64

typedef struct {
    void *map;
    size_t len;
    const float *W;
} noizzen_model;

typedef struct {
    const noizzen_model *m;
    float *S;
    float *st[2];
    int cur;
} noizzen_stream;

static uint32_t crc32_ieee(const unsigned char *p, size_t n) {
    uint32_t table[256];
    for (uint32_t i = 0; i < 256; i++) {
        uint32_t c = i;
        for (int k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320u ^ (c >> 1) : c >> 1;
        table[i] = c;
    }
    uint32_t c = 0xFFFFFFFFu;
    for (size_t i = 0; i < n; i++) c = table[(c ^ p[i]) & 0xFF] ^ (c >> 8);
    return c ^ 0xFFFFFFFFu;
}

static uint32_t u32(const unsigned char *p) {
    uint32_t v;
    memcpy(&v, p, 4);
    return v;
}

/* NULL on any problem: missing or unreadable file, wrong size, another model's or a corrupt file. */
NOIZZEN_API void *noizzen_model_open(const char *path, int check_crc) {
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return NULL;
    struct stat sb;
    const size_t want = HEADER + (size_t)NOIZZEN_N_FLOATS * sizeof(float);
    if (fstat(fd, &sb) != 0 || !S_ISREG(sb.st_mode) || (size_t)sb.st_size != want) {
        close(fd);
        return NULL;
    }
    void *map = mmap(NULL, want, PROT_READ, MAP_PRIVATE, fd, 0);
    close(fd);
    if (map == MAP_FAILED) return NULL;
    const unsigned char *h = (const unsigned char *)map;
    const int ok = memcmp(h, "NOIZZEN1", 8) == 0 && u32(h + 8) == 1 && u32(h + 12) == NOIZZEN_N_FLOATS &&
                   u32(h + 16) == NOIZZEN_CRC && u32(h + 20) == NOIZZEN_STATE && u32(h + 24) == NOIZZEN_STATE_OFF &&
                   (size_t)NOIZZEN_STATE_OFF + NOIZZEN_STATE <= NOIZZEN_N_FLOATS &&
                   (!check_crc || crc32_ieee(h + HEADER, (size_t)NOIZZEN_N_FLOATS * sizeof(float)) == NOIZZEN_CRC);
    noizzen_model *m = ok ? calloc(1, sizeof *m) : NULL;
    if (!m) {
        munmap(map, want);
        return NULL;
    }
    m->map = map;
    m->len = want;
    m->W = (const float *)(h + HEADER);
    return m;
}

NOIZZEN_API void noizzen_model_close(void *mp) {
    noizzen_model *m = mp;
    if (!m) return;
    munmap(m->map, m->len);
    free(m);
}

NOIZZEN_API void noizzen_stream_reset(void *sp) {
    noizzen_stream *s = sp;
    memcpy(s->st[0], s->m->W + NOIZZEN_STATE_OFF, NOIZZEN_STATE * sizeof(float));
    s->cur = 0;
}

NOIZZEN_API void noizzen_stream_close(void *sp) {
    noizzen_stream *s = sp;
    if (!s) return;
    free(s->S);
    free(s->st[0]);
    free(s->st[1]);
    free(s);
}

NOIZZEN_API void *noizzen_stream_open(const void *mp) {
    const noizzen_model *m = mp;
    if (!m) return NULL;
    noizzen_stream *s = calloc(1, sizeof *s);
    if (!s) return NULL;
    s->m = m;
    if (posix_memalign((void **)&s->S, 64, (size_t)NOIZZEN_ARENA * sizeof(float)) ||
        posix_memalign((void **)&s->st[0], 64, NOIZZEN_STATE * sizeof(float)) ||
        posix_memalign((void **)&s->st[1], 64, NOIZZEN_STATE * sizeof(float))) {
        noizzen_stream_close(s);
        return NULL;
    }
    noizzen_stream_reset(s);
    return s;
}

static void run_frame(const float *W, const float *in, const float *st_in, float *out, float *st_out, float *S,
                      float *dbg) {
#if defined(__x86_64__) || defined(__i386__)
    /* Flush denormals to zero for this frame only: decaying recurrent state in near-silence would
     * otherwise slow x86 down by orders of magnitude. The caller's mode is restored. */
    const unsigned int csr = _mm_getcsr();
    _mm_setcsr(csr | 0x8040);
    noizzen_run(W, in, st_in, out, st_out, S, dbg);
    _mm_setcsr(csr);
#else
    noizzen_run(W, in, st_in, out, st_out, S, dbg);
#endif
}

/* One frame (hop) of one call: spec_in[NOIZZEN_SPEC] -> spec_out[NOIZZEN_SPEC]; the stream keeps the
 * recurrent state, swapping its two buffers instead of copying. */
NOIZZEN_API void noizzen_stream_step(void *sp, const float *spec_in, float *spec_out) {
    noizzen_stream *s = sp;
    run_frame(s->m->W, spec_in, s->st[s->cur], spec_out, s->st[s->cur ^ 1], s->S, NULL);
    s->cur ^= 1;
}

/* Explicit-state frame for verification; dbg (NOIZZEN_DBG floats, or NULL) receives every node's output. */
NOIZZEN_API void noizzen_stream_step_state(void *sp, const float *spec_in, const float *state_in, float *spec_out,
                                     float *state_out, float *dbg) {
    noizzen_stream *s = sp;
    run_frame(s->m->W, spec_in, state_in, spec_out, state_out, s->S, dbg);
}

NOIZZEN_API int noizzen_abi_version(void) { return NOIZZEN_ABI; }
NOIZZEN_API int noizzen_spec_size(void) { return NOIZZEN_SPEC; }
NOIZZEN_API int noizzen_state_size(void) { return NOIZZEN_STATE; }
NOIZZEN_API long noizzen_dbg_size(void) { return NOIZZEN_DBG; }
NOIZZEN_API long noizzen_arena_size(void) { return NOIZZEN_ARENA; }
