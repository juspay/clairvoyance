# Krisp BVC Integration Guide for Clairvoyance

## Summary

**The Problem:** The Breeze Automatic voice agent's effectiveness is currently limited by its sensitivity to background noise, which degrades the user experience and leads to transcription errors.

**The Solution:** This document outlines a plan to integrate Krisp's AI-powered Background Voice Cancellation (BVC) technology. By using the `pipecat-ai` library's built-in `KrispFilter` and the optimal 16kHz Krisp model, we can clean the audio at the source.

**The Impact:** This integration will significantly improve Speech-to-Text accuracy, resulting in a more reliable and professional voice agent. The expected performance overhead is minimal and a worthy trade-off for the substantial gain in quality and robustness.

---

This document provides a comprehensive overview of the integration of Krisp's Background Voice Cancellation (BVC) technology into the Clairvoyance voice agent.

## 1. What is Krisp?

Krisp is an AI-powered noise-cancellation technology that removes background noise and echo from audio in real-time. Famously used by platforms like Discord to ensure clear communication, its key feature, Background Voice Cancellation (BVC), is specifically designed to isolate the primary speaker's voice, eliminating any other background voices or noises. This results in crystal-clear audio, which is essential for high-accuracy voice applications.

## 2. Why Integrate Krisp into Clairvoyance?

The primary motivation for this integration is to solve a core operational issue: **the Breeze Automatic agent is sensitive to background noise**, which can lead to incorrect transcriptions and flawed agent behavior.

By integrating Krisp, we aim to significantly enhance the **audio quality** at the very beginning of the processing pipeline. This has several critical benefits:

*   **Improved STT Accuracy:** By providing the Speech-to-Text (STT) engine with a clean audio signal, we drastically reduce transcription errors. This is the most important benefit, as it directly improves the agent's ability to understand and respond to the user correctly.
*   **Enhanced User Experience:** A cleaner audio signal makes the conversation feel more professional and focused, improving the overall user experience and trust in the agent.
*   **Robust Performance:** The agent will become more reliable in noisy, real-world environments (e.g., call centers, open offices, public spaces), expanding its effective use cases.

## 3. How Krisp Will Be Integrated

The integration is simpler than previously described. The `KrispFilter` is a built-in component of the `pipecat-ai` library, available via an optional installation. We do **not** need to create any new processor files.

The correct plan is as follows:

| Step | Action | File(s) to Modify |
| :--- | :--- | :--- |
| 1 | **Add Krisp Dependency** | `requirements.txt` |
| 2 | **Add Krisp Configuration** | `app/core/config.py` |
| 3 | **Integrate `KrispFilter` into Pipeline** | `app/agents/voice/automatic/__init__.py` |

### Step 1: Add the Krisp Dependency

Modify `requirements.txt` to include the optional Krisp components for `pipecat-ai`.

```text
# In requirements.txt
...
pipecat-ai[krisp]
...
```

### Step 2: Add Krisp Configuration

Modify `app/core/config.py` to manage the path to the Krisp model file.

```python
# In app/core/config.py
...
KRISP_MODEL_PATH = os.getenv("KRISP_MODEL_PATH")
...
```

### Step 3: Integrate the `KrispFilter` into the Pipeline

Modify `app/agents/voice/automatic/__init__.py` to import and use the `KrispFilter`.

```python
# In app/agents/voice/automatic/__init__.py
...
from pipecat.audio.filters.krisp_filter import KrispFilter
from app.core import config
...

# ... inside the main() function
daily_params = DailyParams(...)

# Note: The original documentation mentioned replacing KrispFilter with
# NoiseFilterFromKrisp for newer SDKs. Assuming we are using the version
# bundled with pipecat, KrispFilter is the correct class to use.
if config.KRISP_MODEL_PATH:
    logger.info(f"Krisp noise reduction enabled with model: {config.KRISP_MODEL_PATH}")
    daily_params.audio_in_filter = KrispFilter(model_path=config.KRISP_MODEL_PATH)
elif config.ENABLE_NOISE_REDUCE_FILTER:
    logger.info("Default noise reduction filter enabled.")
    daily_params.audio_in_filter = NoisereduceFilter()
else:
    logger.info("No audio input filter enabled.")

transport = DailyTransport(
    args.url,
    args.token,
    "Breeze Automatic Voice Agent",
    daily_params,
)
...
```

## 4. Pipeline Architecture and Impact

The audio flow remains the same, but the implementation is much cleaner as it uses a built-in library feature.

1.  **Audio Input (`DailyTransport`)**: Captures raw audio.
2.  **Krisp Noise Filter (`KrispFilter`)**: The raw audio is immediately passed to the built-in Krisp filter, which is now part of the `DailyTransport`'s configuration.
3.  **VAD & STT**: The clean audio is processed.
4.  **LLM, Tools, TTS**: The rest of the pipeline continues as normal.

### Expected Performance Impact

The integration of Krisp will introduce some additional CPU load, as it is an active audio processing filter. However, the chosen model (`hs.c6.w.s.087e35`) is specifically designed for efficiency.

*   **Low CPU Usage:** The Krisp documentation notes that this model is optimized for "low CPU usage" and is suitable for "power-limited systems."
*   **Server Environment:** Given that the Clairvoyance agent is deployed in a server environment (GCP), the moderate CPU increase is considered a highly acceptable trade-off for the significant improvement in audio quality and STT accuracy. The impact on overall system performance is expected to be minimal.

## 5. Model Selection: Choosing the Right Krisp Model

The Clairvoyance audio pipeline operates at a **16kHz sampling rate**, as defined in the `SileroVADAnalyzer` configuration. Based on this, the recommended model is the **Medium Quality Outbound BVC model**.

*   **Model Name:** `hs.c6.w.s.087e35`
*   **Size:** 8.6MB
*   **Sampling Rate:** 16kHz

### Why this model is the best choice:

1.  **Matching Sampling Rate:** This model's 16kHz operational sampling rate is a **perfect match** for our pipeline. This avoids any unnecessary downsampling or upsampling, which preserves audio integrity and ensures maximum performance.

2.  **Correct Use Case (Outbound BVC):** The model is an **Outbound (Microphone)** model with **Background Voice Cancellation (BVC)**. It is specifically trained to process a single speaker's voice from a microphone and remove both background noise and other voices, which is exactly what our use case requires.

3.  **Optimal Performance:** By matching the sampling rate, we achieve the best possible audio quality for our specific pipeline while ensuring efficient CPU usage.

## 6. System Requirements

While the Krisp SDK is highly optimized, it's useful to understand the general system requirements for the chosen Medium-Quality BVC model.

*   **Recommended CPU:** Intel Core i5 (7th Gen+) / AMD Ryzen 5 or better.
*   **Recommended RAM:** 8 GB or more.
*   **Expected CPU Load:** Approximately 5-13% overhead on a client machine.

Given that the Clairvoyance agent runs in a server environment (GCP), these requirements are easily met, and the performance impact on the server will be negligible.

## 7. Deployment and Testing Strategy

The strategy is now much simpler because we are using a built-in library feature.

### 7.1. Production Deployment (GCP & Docker)

**Prerequisites:**

1.  You still need the Krisp model file (e.g., `model.kef`). Create a directory named `krisp_assets` in the project root and place the model file inside it.

**Required `Dockerfile` Changes:**

The only change needed in the `Dockerfile` is to copy the model file into the image. The SDK itself will be handled by `pip`.

```Dockerfile
# ... (previous Dockerfile commands)

# Install Python dependencies from requirements.txt
# This will now automatically pull in the Krisp SDK
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy Krisp model assets into the image
COPY krisp_assets/ /app/krisp_assets/

# Copy application code
COPY . .

# ... (subsequent Dockerfile commands)
```

**Configuration in GCP:**

When deploying the container, set the environment variable to point to the model file's location inside the container:
*   **Variable Name:** `KRISP_MODEL_PATH`
*   **Value:** `/app/krisp_assets/model.kef`

### 7.2. Local Development and Testing

1.  **Place Krisp Model:**
    *   Create the `krisp_assets` directory in the project root.
    *   Place the `.kef` model file inside this directory.

2.  **Install the Library:**
    *   Activate your Python virtual environment (e.g., `source venv/bin/activate`).
    *   Modify your `requirements.txt` to include `pipecat-ai[krisp]`.
    *   Run `pip install -r requirements.txt`.

3.  **Configure the Environment:**
    *   Open or create your `.env` file.
    *   Add the following line, replacing the placeholder with the **absolute path** to your model file:
        ```
        KRISP_MODEL_PATH=/Users/your-name/path-to-project/krisp_assets/model.kef
        ```

4.  **Run the Application:**
    *   Run the application locally (`python run.py`). It will now use the `KrispFilter` with the model specified in your `.env` file.

## 8. References

*   [Krisp SDK & Daily/Pipecat Integration](https://sdk-docs.krisp.ai/docs/daily-pipecat)
*   [Krisp SDK Model Selection Guide](https://sdk-docs.krisp.ai/docs/krisp-audio-sdk-model-selection-guide)
*   [Pipecat Krisp Feature Guide](https://docs.pipecat.ai/guides/features/krisp)
*   [Pipecat KrispFilter Documentation](https://docs.pipecat.ai/server/utilities/audio/krisp-filter)
