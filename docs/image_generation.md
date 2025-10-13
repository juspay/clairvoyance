# ComfyUI Image Generation Integration

This document provides comprehensive guidance for setting up and using ComfyUI image generation within the Clairvoyance voice agent platform.

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Installation & Setup](#installation--setup)
4. [Configuration](#configuration)
5. [Voice Integration](#voice-integration)
6. [API Reference](#api-reference)
7. [Troubleshooting](#troubleshooting)
8. [Performance Optimization](#performance-optimization)
9. [Advanced Usage](#advanced-usage)
10. [Future Enhancements](#future-enhancements)

## Overview

The ComfyUI integration enables Clairvoyance voice agents to generate images in real-time based on voice commands. This is particularly powerful for creating advertisements, marketing materials, and custom visual content through natural language interactions.

### Key Features

- **Voice-Activated Image Generation**: Generate images through natural speech
- **Advertisement Specialization**: Optimized workflows for marketing content
- **Real-time Processing**: WebSocket-based progress monitoring
- **Multiple Formats**: Support for various image sizes and styles
- **Error Handling**: Robust fallback mechanisms and user feedback

## Architecture

### System Components

```
Voice Input → Speech-to-Text → LLM Processing → ComfyUI Tool → Image Generation → Response
```

### File Structure

```
app/
├── agents/voice/automatic/
│   ├── services/comfyui/
│   │   ├── __init__.py
│   │   └── client.py                 # ComfyUI client implementation
│   └── tools/comfyui/
│       ├── __init__.py
│       ├── image_generation.py       # Tool functions
│       └── tools.py                  # Tool definitions
├── core/
│   └── config.py                     # Configuration variables
└── requirements.txt                  # Dependencies
```

### Data Flow

1. **Voice Input**: User speaks image generation request
2. **Processing**: LLM extracts parameters (prompt, style, size, etc.)
3. **Tool Selection**: Agent selects appropriate ComfyUI tool
4. **Workflow Building**: Client constructs ComfyUI workflow
5. **Generation**: ComfyUI processes image with real-time monitoring
6. **Response**: Generated image URL returned to user

## Installation & Setup

### Prerequisites

- **Python 3.10+** (tested with 3.13.5)
- **4GB+ available disk space** (for models)
- **8GB+ RAM** recommended
- **GPU support** (optional but recommended)
  - NVIDIA GPU with 4GB+ VRAM (CUDA)
  - Apple Silicon (MPS)
  - CPU-only supported but slower

### Step 1: Clone ComfyUI

```bash
# Navigate to parent directory of Clairvoyance
cd /path/to/parent/directory

# Clone ComfyUI repository
git clone https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI
```

### Step 2: Create Virtual Environment

```bash
# Create virtual environment
python3 -m venv comfyui_venv

# Activate virtual environment
source comfyui_venv/bin/activate  # macOS/Linux
# OR
comfyui_venv\Scripts\activate     # Windows
```

### Step 3: Install Dependencies

```bash
# Upgrade pip
python -m pip install --upgrade pip

# Install ComfyUI requirements
pip install -r requirements.txt
```

### Step 4: Download Models

Download at least one Stable Diffusion model to `models/checkpoints/`:

**Recommended Models:**

```bash
# Navigate to checkpoints directory
cd models/checkpoints/

# Download Stable Diffusion v1.5 (4GB) - Good general model
curl -L "https://huggingface.co/runwayml/stable-diffusion-v1-5/resolve/main/v1-5-pruned-emaonly.ckpt" -o "v1-5-pruned-emaonly.ckpt"

# Alternative: Realistic Vision (better for realistic images)
# curl -L "https://civitai.com/api/download/models/130072" -o "realisticVisionV60_v60B1VAE.safetensors"
```

### Step 5: Start ComfyUI Server

```bash
# Start ComfyUI server
source comfyui_venv/bin/activate
python main.py --listen 0.0.0.0 --port 8188
```

**Server startup indicators:**
- `Device: mps` (Apple Silicon) or `Device: cuda` (NVIDIA)
- `Using sub quadratic optimization for attention`
- Server accessible at `http://localhost:8188`

### Step 6: Verify Installation

```bash
# Test system stats endpoint
curl http://localhost:8188/system_stats

# Test object info endpoint
curl http://localhost:8188/object_info | head -5
```

## Configuration

### Environment Variables

Add to your `.env` file:

```bash
# =================================================================
# ComfyUI Configuration
# =================================================================
ENABLE_COMFYUI=true
COMFYUI_BASE_URL=http://localhost:8188
COMFYUI_WEBSOCKET_URL=ws://localhost:8188/ws
COMFYUI_TIMEOUT=300
COMFYUI_DEFAULT_MODEL=v1-5-pruned-emaonly.ckpt
COMFYUI_DEFAULT_STEPS=20
COMFYUI_DEFAULT_CFG=8.0
COMFYUI_DEFAULT_WIDTH=1024
COMFYUI_DEFAULT_HEIGHT=1024
```

### Configuration Parameters

| Parameter | Description | Default | Range |
|-----------|-------------|---------|-------|
| `ENABLE_COMFYUI` | Enable/disable ComfyUI integration | `false` | `true/false` |
| `COMFYUI_BASE_URL` | ComfyUI server URL | `http://localhost:8188` | Any valid URL |
| `COMFYUI_WEBSOCKET_URL` | WebSocket URL for real-time monitoring | `ws://localhost:8188/ws` | Any valid WebSocket URL |
| `COMFYUI_TIMEOUT` | Generation timeout in seconds | `300` | `60-600` |
| `COMFYUI_DEFAULT_MODEL` | Default checkpoint model | `v1-5-pruned-emaonly.ckpt` | Any model in checkpoints/ |
| `COMFYUI_DEFAULT_STEPS` | Default sampling steps | `20` | `10-50` |
| `COMFYUI_DEFAULT_CFG` | Default CFG scale | `8.0` | `1.0-20.0` |
| `COMFYUI_DEFAULT_WIDTH` | Default image width | `1024` | `512-2048` |
| `COMFYUI_DEFAULT_HEIGHT` | Default image height | `1024` | `512-2048` |

## Voice Integration

### Available Voice Commands

**Advertisement Generation:**
- *"Make an ad for me where shoes are there"*
- *"Generate an advertisement for running shoes with a modern style"*
- *"Create a luxury watch advertisement"*
- *"Make a minimalist poster for coffee"*

**Custom Image Generation:**
- *"Generate a custom image of a sunset over mountains"*
- *"Create an image of a futuristic city"*
- *"Generate a portrait of a woman in vintage style"*

### Tool Definitions

#### 1. Advertisement Image Tool

```python
{
    "name": "generate_advertisement_image",
    "description": "Generate an advertisement image using ComfyUI based on a text description. Perfect for creating marketing materials, product ads, and promotional content.",
    "parameters": {
        "prompt": "Detailed description of the advertisement",
        "product_type": "Type of product (shoes, clothing, electronics, etc.)",
        "style": "Advertisement style (modern, vintage, minimalist, luxury, casual)",
        "width": "Image width in pixels (default: 1024)",
        "height": "Image height in pixels (default: 1024)"
    }
}
```

#### 2. Custom Image Tool

```python
{
    "name": "generate_custom_image",
    "description": "Generate a custom image using ComfyUI with advanced parameters. Use this for general image generation beyond advertisements.",
    "parameters": {
        "prompt": "Detailed text description of the image",
        "negative_prompt": "Things to avoid (default: 'text, watermark, low quality, worst quality')",
        "width": "Image width in pixels (default: 1024)",
        "height": "Image height in pixels (default: 1024)",
        "steps": "Number of denoising steps (default: 20)",
        "cfg": "Classifier-free guidance scale (default: 8.0)",
        "sampler_name": "Sampling method (default: 'euler')"
    }
}
```

### Tool Integration Flow

1. **Tool Selection**: LLM determines appropriate tool based on user intent
2. **Parameter Extraction**: LLM extracts relevant parameters from voice input
3. **Tool Execution**: ComfyUI client processes the request
4. **Progress Monitoring**: WebSocket connection tracks generation progress
5. **Result Processing**: Generated image URLs are returned to user
6. **Voice Response**: Agent provides feedback with image information

## API Reference

### ComfyUI Client Methods

#### `ComfyUIClient`

Primary client class for interacting with ComfyUI server.

```python
class ComfyUIClient:
    def __init__(self, base_url=None, websocket_url=None, timeout=300)

    async def get_system_stats(self) -> Dict[str, Any]
    async def queue_prompt(self, workflow: Dict[str, Any]) -> str
    async def wait_for_completion(self, prompt_id: str) -> Dict[str, Any]
    async def get_history(self, prompt_id: str) -> Dict[str, Any]
    async def get_image_urls(self, history: Dict[str, Any]) -> List[str]
    async def generate_image(self, prompt: str, workflow_template: str = "text_to_image", **kwargs) -> Tuple[List[str], Dict[str, Any]]
    def build_workflow(self, prompt: str, template: str = "text_to_image", **kwargs) -> Dict[str, Any]
```

#### Key Methods

**`queue_prompt(workflow)`**
- Submits a workflow to ComfyUI for processing
- Returns prompt ID for tracking
- Raises exception on server errors

**`wait_for_completion(prompt_id)`**
- Monitors generation progress via WebSocket
- Returns execution history when complete
- Handles timeout and error conditions

**`generate_image(prompt, template, **kwargs)`**
- High-level image generation method
- Builds workflow, queues prompt, waits for completion
- Returns tuple of (image_urls, history)

### Workflow Templates

#### Text-to-Image Workflow

```python
{
    "1": {  # CheckpointLoaderSimple
        "inputs": {"ckpt_name": "v1-5-pruned-emaonly.ckpt"},
        "class_type": "CheckpointLoaderSimple"
    },
    "2": {  # EmptyLatentImage
        "inputs": {"width": 1024, "height": 1024, "batch_size": 1},
        "class_type": "EmptyLatentImage"
    },
    "3": {  # CLIPTextEncode (Positive)
        "inputs": {"text": "your prompt here"},
        "class_type": "CLIPTextEncode"
    },
    "4": {  # CLIPTextEncode (Negative)
        "inputs": {"text": "text, watermark, low quality, worst quality"},
        "class_type": "CLIPTextEncode"
    },
    "5": {  # KSampler
        "inputs": {
            "seed": -1,
            "steps": 20,
            "cfg": 8.0,
            "sampler_name": "euler",
            "scheduler": "normal",
            "denoise": 1.0,
            "model": ["1", 0],
            "positive": ["3", 0],
            "negative": ["4", 0],
            "latent_image": ["2", 0]
        },
        "class_type": "KSampler"
    },
    "6": {  # VAEDecode
        "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
        "class_type": "VAEDecode"
    },
    "7": {  # SaveImage
        "inputs": {"filename_prefix": "ComfyUI", "images": ["6", 0]},
        "class_type": "SaveImage"
    }
}
```

#### Advertisement Workflow

Enhanced text-to-image workflow with advertisement-specific prompt engineering:

```python
enhanced_prompt = f"professional {style} advertisement for {product_type}, {prompt}, high quality, commercial photography, studio lighting, clean background"
```

## Troubleshooting

### Common Issues

#### 1. ComfyUI Won't Start

**Error:** `externally-managed-environment`
```bash
# Solution: Use virtual environment
python3 -m venv comfyui_venv
source comfyui_venv/bin/activate
pip install -r requirements.txt
```

**Error:** `ModuleNotFoundError: No module named 'torch'`
```bash
# Solution: Install PyTorch manually
pip install torch torchvision torchaudio
```

#### 2. Model Loading Issues

**Error:** `No checkpoints found`
```bash
# Solution: Verify model placement
ls -la models/checkpoints/
# Should contain .ckpt or .safetensors files
```

**Error:** `CUDA out of memory`
```bash
# Solutions:
# 1. Reduce image size
# 2. Lower batch size
# 3. Use CPU mode: python main.py --cpu
# 4. Use memory optimization: python main.py --use-split-cross-attention
```

#### 3. Network Connection Issues

**Error:** `Connection refused`
```bash
# Check if ComfyUI is running
curl http://localhost:8188/system_stats

# Check port availability
lsof -i :8188
```

**Error:** `WebSocket connection failed`
- Ensure WebSocket URL uses `ws://` not `http://`
- Check firewall settings
- Verify ComfyUI server is accepting connections

#### 4. Generation Timeouts

**Error:** `ComfyUI generation timeout`
- Increase `COMFYUI_TIMEOUT` value
- Use fewer generation steps
- Reduce image resolution
- Check server load

### Debugging Steps

1. **Check ComfyUI Status**
   ```bash
   curl http://localhost:8188/system_stats | jq
   ```

2. **Monitor ComfyUI Logs**
   ```bash
   # Check ComfyUI terminal output for errors
   tail -f /path/to/comfyui/logs
   ```

3. **Test Manual Generation**
   ```bash
   # Test basic workflow via API
   curl -X POST http://localhost:8188/prompt \
     -H "Content-Type: application/json" \
     -d '{"prompt": {}, "client_id": "test"}'
   ```

4. **Check Clairvoyance Logs**
   ```python
   # Look for ComfyUI-related log entries
   grep -i "comfyui" /path/to/clairvoyance/logs
   ```

## Performance Optimization

### Hardware Optimization

#### GPU Settings

**NVIDIA GPU:**
```bash
# Check GPU memory
nvidia-smi

# Optimize for high VRAM (>8GB)
python main.py --gpu-only

# Optimize for low VRAM (<6GB)
python main.py --lowvram
```

**Apple Silicon:**
```bash
# MPS is automatically detected
# Monitor memory usage
vm_stat | grep "Pages free"
```

#### Memory Management

**High Memory Systems (>16GB RAM):**
- Use default settings
- Enable model caching: `--disable-xformers-memory-efficient-attention`

**Low Memory Systems (<8GB RAM):**
```bash
python main.py --lowvram --cpu
```

### Generation Speed

#### Fast Generation Settings

```python
# Optimized for speed
{
    "steps": 15,          # Reduced from 20
    "cfg": 7.0,          # Slightly lower
    "sampler_name": "dpm_fast",  # Faster sampler
    "scheduler": "simple"
}
```

#### Quality Generation Settings

```python
# Optimized for quality
{
    "steps": 30,          # Increased steps
    "cfg": 9.0,          # Higher guidance
    "sampler_name": "dpmpp_2m",  # High-quality sampler
    "scheduler": "karras"
}
```

### Model Selection

#### Performance vs Quality

| Model | Size | Speed | Quality | Use Case |
|-------|------|-------|---------|----------|
| SD 1.5 | 4GB | Fast | Good | General purpose |
| SD XL | 7GB | Medium | High | High quality images |
| Realistic Vision | 4GB | Fast | High (realistic) | Product photos |
| DreamShaper | 4GB | Fast | High (artistic) | Creative content |

## Advanced Usage

### Custom Workflows

#### ControlNet Integration

```python
def build_controlnet_workflow(prompt, control_image, control_type="canny"):
    return {
        # Base workflow + ControlNet nodes
        "8": {  # ControlNetLoader
            "inputs": {"control_net_name": f"control_v11p_sd15_{control_type}.pth"},
            "class_type": "ControlNetLoader"
        },
        "9": {  # ControlNetApply
            "inputs": {
                "conditioning": ["3", 0],
                "control_net": ["8", 0],
                "image": control_image,
                "strength": 1.0
            },
            "class_type": "ControlNetApply"
        }
    }
```

#### LoRA Integration

```python
def build_lora_workflow(prompt, lora_name, strength=0.8):
    workflow = base_workflow.copy()
    workflow["10"] = {  # LoraLoader
        "inputs": {
            "model": ["1", 0],
            "clip": ["1", 1],
            "lora_name": lora_name,
            "strength_model": strength,
            "strength_clip": strength
        },
        "class_type": "LoraLoader"
    }
    return workflow
```

### Batch Processing

```python
async def generate_image_batch(prompts, **kwargs):
    """Generate multiple images concurrently."""
    tasks = []
    for prompt in prompts:
        task = client.generate_image(prompt, **kwargs)
        tasks.append(task)

    results = await asyncio.gather(*tasks, return_exceptions=True)
    return results
```

### Custom Samplers

```python
ADVANCED_SAMPLERS = {
    "speed": {
        "sampler_name": "dpm_fast",
        "scheduler": "simple",
        "steps": 15
    },
    "quality": {
        "sampler_name": "dpmpp_2m",
        "scheduler": "karras",
        "steps": 30
    },
    "artistic": {
        "sampler_name": "euler_ancestral",
        "scheduler": "normal",
        "steps": 25
    }
}
```

## Future Enhancements

### Planned Features

1. **Multi-Model Support**
   - Automatic model selection based on content type
   - Model switching for different art styles
   - SDXL and other architecture support

2. **Advanced Voice Integration**
   - Style learning from previous generations
   - User preference memory
   - Context-aware prompt enhancement

3. **Real-time Previews**
   - Progressive image generation display
   - Low-resolution preview streaming
   - Interactive refinement during generation

4. **Business Features**
   - Brand consistency enforcement
   - Template-based generation
   - Bulk processing workflows
   - Quality assurance automation

### Integration Opportunities

1. **External APIs**
   - Unsplash/Pexels for reference images
   - Brand asset databases
   - Color palette generators

2. **Post-Processing**
   - Automatic upscaling
   - Background removal
   - Format conversion
   - Watermark addition

3. **Analytics**
   - Generation time tracking
   - Usage pattern analysis
   - Quality metrics
   - Cost optimization

### Scalability Considerations

1. **Horizontal Scaling**
   - Multiple ComfyUI instances
   - Load balancing
   - Queue management
   - Distributed storage

2. **Cloud Integration**
   - AWS/GCP GPU instances
   - Container orchestration
   - Auto-scaling policies
   - Cost monitoring

---

## Support & Resources

### Official Documentation
- [ComfyUI GitHub](https://github.com/comfyanonymous/ComfyUI)
- [ComfyUI Community](https://comfyui.com/)
- [Stable Diffusion Guide](https://stability.ai/)

### Model Resources
- [Hugging Face Models](https://huggingface.co/models?pipeline_tag=text-to-image)
- [Civitai Community](https://civitai.com/)
- [OpenArt Models](https://openart.ai/)

### Development
- [Clairvoyance Repository](/)
- [Pipecat Framework](https://github.com/pipecat-ai/pipecat)
- [FastAPI Documentation](https://fastapi.tiangolo.com/)

---

*This document was generated as part of the Clairvoyance ComfyUI integration project. For updates and contributions, please refer to the main project repository.*