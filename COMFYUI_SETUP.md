# ComfyUI Setup Guide for Clairvoyance

This guide provides multiple methods to set up ComfyUI with fal-API integration for the Clairvoyance voice-activated AI assistant.

## 🚀 Quick Setup (Recommended)

### Option 1: Automated Setup Script

```bash
# Navigate to clairvoyance directory
cd /path/to/clairvoyance

# Run the automated setup script
./setup_comfyui.sh

# Follow the prompts to enter your fal.ai API key
```

**What the script does:**
- ✅ Installs ComfyUI in the correct directory structure
- ✅ Sets up Python virtual environment with all dependencies
- ✅ Installs ComfyUI-fal-API custom nodes from [gokayfem/ComfyUI-fal-API](https://github.com/gokayfem/ComfyUI-fal-API)
- ✅ Configures fal.ai API key
- ✅ Updates Clairvoyance environment variables
- ✅ Creates startup scripts
- ✅ Tests the installation

### Option 2: Manual Setup

If you prefer manual installation or the script doesn't work in your environment:

## 📋 Prerequisites

- **Python 3.8+**
- **Git**
- **fal.ai API Key** (get from [fal.ai/dashboard/keys](https://fal.ai/dashboard/keys))
- **8GB+ RAM** (recommended for model loading)
- **10GB+ free disk space** (for models and dependencies)

## 🛠️ Manual Installation Steps

### 1. Install ComfyUI

```bash
# Create directory structure
mkdir -p /Users/$(whoami)/work_dir/temp
cd /Users/$(whoami)/work_dir/temp

# Clone ComfyUI
git clone https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI

# Create virtual environment
python3 -m venv comfyui_venv
source comfyui_venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Install PyTorch (adjust for your system)
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
```

### 2. Install fal-API Custom Nodes

```bash
# Navigate to custom nodes directory
cd custom_nodes

# Clone fal-API nodes
git clone https://github.com/gokayfem/ComfyUI-fal-API.git
cd ComfyUI-fal-API

# Install fal-API requirements
pip install -r requirements.txt
```

### 3. Configure fal-API

```bash
# Edit config.ini
nano config.ini

# Replace <your_fal_api_key_here> with your actual API key:
[API]
FAL_KEY = your_actual_fal_api_key_here
```

### 4. Update Clairvoyance Configuration

Add these lines to your `.env` file in the clairvoyance directory:

```bash
# ComfyUI Configuration
ENABLE_COMFYUI=true
COMFYUI_BASE_URL=http://localhost:8188
COMFYUI_WEBSOCKET_URL=ws://localhost:8188/ws
COMFYUI_TIMEOUT=300
```

### 5. Test Installation

```bash
# Start ComfyUI
cd /Users/$(whoami)/work_dir/temp/ComfyUI
source comfyui_venv/bin/activate
python main.py --listen 0.0.0.0 --port 8188
```

Visit [http://localhost:8188](http://localhost:8188) to verify ComfyUI is running.

## 🔗 Integration Details

### Current Integration Architecture

```
Clairvoyance Voice AI
├── app/agents/voice/automatic/
│   ├── services/comfyui/client.py          # ComfyUI service client
│   └── tools/comfyui/
│       ├── image_generation.py              # Image generation logic
│       └── tools.py                         # Function schemas
├── app/core/config.py                       # Configuration settings
└── ComfyUI/ (external)
    ├── custom_nodes/ComfyUI-fal-API/        # fal-API nodes
    └── output/                              # Generated images
```

### How It Works

1. **Voice Input** → Clairvoyance processes user speech
2. **LLM Analysis** → GPT-4.1 determines if image generation is needed
3. **Function Call** → Calls `generate_advertisement_image()` or `mask_and_edit_object()`
4. **ComfyUI Service** → Sends workflow to ComfyUI via HTTP/WebSocket
5. **fal-API Processing** → ComfyUI uses fal-API nodes to call fal.ai services
6. **Image Return** → Generated images returned to voice interface

### Available fal-API Models

The setup includes these image generation capabilities:

**Text-to-Image:**
- Flux Pro/Dev/Schnell
- Recraft V3
- Sana
- Ideogram v3
- HiDream Full

**Image-to-Image Editing:**
- Flux Pro Kontext (Context-aware editing)
- Qwen Image Edit (Object-focused editing)
- SeedEdit V3 (Precise modifications)

**Advanced Features:**
- ControlNets for guided generation
- LoRA support for style transfer
- Multi-image composition
- Size scaling (make objects bigger/smaller)

## 🎯 Usage Examples

Once setup is complete, you can use voice commands like:

```
"Create an ad for me where you show a whiskey bottle. The bottle should be placed on a dark wooden bar with a sophisticated lounge background."

"I have my own bottle image that I want to use instead of generating one."

"Change the background to an upscale party setting with elegant decor."

"Make the whiskey bottle smaller and show more of the party setting around it."
```

## 🔧 Troubleshooting

### Common Issues

**ComfyUI won't start:**
```bash
# Check Python version
python3 --version  # Should be 3.8+

# Check virtual environment
source /Users/$(whoami)/work_dir/temp/ComfyUI/comfyui_venv/bin/activate
which python  # Should point to venv
```

**fal-API nodes missing:**
```bash
# Verify nodes are installed
ls /Users/$(whoami)/work_dir/temp/ComfyUI/custom_nodes/ComfyUI-fal-API/nodes/

# Check fal-client installation
pip list | grep fal-client
```

**API key issues:**
```bash
# Check config file
cat /Users/$(whoami)/work_dir/temp/ComfyUI/custom_nodes/ComfyUI-fal-API/config.ini

# Verify key format (should not contain < >)
# Should look like: FAL_KEY = fal-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**Memory issues:**
- Ensure at least 8GB RAM available
- Close other applications
- Restart ComfyUI if models fail to load

**Network issues:**
- Check firewall settings for port 8188
- Ensure ComfyUI is accessible at localhost:8188
- Verify Clairvoyance can reach ComfyUI service

### Log Locations

- **ComfyUI logs:** Terminal output when running `python main.py`
- **Clairvoyance logs:** Check application logs for ComfyUI service errors
- **System logs:** Check system console for memory/performance issues

## 📊 Performance Optimization

### System Requirements

**Minimum:**
- 8GB RAM
- 4 CPU cores
- 10GB free disk space

**Recommended:**
- 16GB+ RAM
- 8+ CPU cores
- 50GB+ free disk space
- GPU with 8GB+ VRAM (for local models, if needed)

### Optimization Tips

1. **Use SSD storage** for faster model loading
2. **Close unnecessary applications** when generating images
3. **Monitor memory usage** - restart ComfyUI if memory leaks occur
4. **Use appropriate model sizes** based on your hardware

## 🔒 Security Notes

- Keep your fal.ai API key secure
- Don't commit API keys to version control
- Use environment variables in production
- Monitor API usage to avoid unexpected charges

## 📚 Additional Resources

- [ComfyUI Documentation](https://github.com/comfyanonymous/ComfyUI)
- [ComfyUI-fal-API Repository](https://github.com/gokayfem/ComfyUI-fal-API)
- [fal.ai Documentation](https://fal.ai/docs)
- [fal.ai Models](https://fal.ai/models)

## 🆘 Support

If you encounter issues:

1. **Check this documentation** for common solutions
2. **Review logs** for specific error messages
3. **Update dependencies** if needed
4. **Restart services** (ComfyUI, then Clairvoyance)
5. **Check system resources** (RAM, disk space)

## 🚀 Next Steps

After successful setup:

1. **Test basic functionality** with simple image generation
2. **Explore voice commands** for different image types
3. **Experiment with image editing features**
4. **Monitor performance** and optimize as needed
5. **Set up system service** for automatic startup (optional)

---

**Setup completed successfully? You're ready to generate images with voice commands! 🎉**