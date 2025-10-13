# Claude Development Guide

## Image Generation Development Strategy

### 🔋 **API Call Minimization Strategy**

To minimize fal.ai API costs during development, we implement the following approach:

#### **1. Development Modes**
```python
# Environment-based configuration
DEVELOPMENT_MODE = os.getenv('DEVELOPMENT_MODE', 'false').lower() == 'true'
USE_MOCK_IMAGES = os.getenv('USE_MOCK_IMAGES', 'false').lower() == 'true'
```

#### **2. Mock Image Generation for Testing**
```python
# In development, return pre-generated sample images instead of API calls
if DEVELOPMENT_MODE and USE_MOCK_IMAGES:
    logger.info("Using mock image for development - skipping fal.ai API call")
    # Return mock image URL to save API costs during development
    image_urls = [_get_mock_image_url()]
    await asyncio.sleep(0.5)  # Simulate brief generation time

def _get_mock_image_url() -> str:
    """Return a mock image URL for development."""
    mock_images = [
        "fal_flux_dev__00001_.png",
        "fal_flux_dev__00002_.png",
        "fal_flux_dev__00003_.png",
        "fal_flux_dev__00004_.png",
        "fal_flux_dev__00005_.png"
    ]
    filename = random.choice(mock_images)
    return f"/api/v1/images/comfyui?filename={filename}"
```

#### **3. Generated Image Caching**
- Store generated images locally for reuse
- Cache by prompt hash to avoid duplicate generations
- Use cached images for repeated testing scenarios

#### **4. Development Best Practices**

##### **For UI Development:**
- Use static sample images stored in `/static/samples/`
- Test UI components without API calls
- Only generate real images for final validation

##### **For Workflow Testing:**
- Mock the image generation service in tests
- Test conversation flow without image generation
- Use placeholder URLs for UI component testing

##### **For Integration Testing:**
- Limit real API calls to essential test cases
- Use simple prompts to minimize processing costs
- Test with single images rather than multiple variants

### 🖼️ **Generated Images for Debugging**

#### **Image Storage Strategy**
```
/Users/anurag.dwivedi/work_dir/temp/ComfyUI/output/
├── samples/          # Pre-generated sample images for development
├── debug/            # Images generated during debugging sessions
├── test/             # Images from automated tests
└── production/       # Real user-generated images
```

#### **Debug Image Management**
- Generate images only when testing specific features
- Reuse generated images across debugging sessions
- Document image generation context for future reference

#### **Debugging Workflow**
1. **Initial Setup**: Generate 3-5 sample images for different scenarios
2. **Development**: Use sample images for UI/UX development
3. **Testing**: Generate new images only for new features
4. **Production**: Enable real-time generation

### 🔧 **Implementation Guidelines**

#### **Environment Configuration**
```bash
# .env for development
DEVELOPMENT_MODE=true
USE_MOCK_IMAGES=true
FAL_API_CALLS_LIMIT=10  # Daily limit for dev

# .env for production
DEVELOPMENT_MODE=false
USE_MOCK_IMAGES=false
```

#### **Code Implementation Pattern**
```python
async def generate_image_with_caching(prompt: str, **kwargs):
    # Check if we're in development mode
    if config.DEVELOPMENT_MODE and config.USE_MOCK_IMAGES:
        return get_sample_image_for_prompt(prompt)

    # Check cache first
    cached_image = get_cached_image(prompt_hash(prompt))
    if cached_image:
        return cached_image

    # Only make API call if necessary
    if should_generate_new_image():
        return await fal_ai_generate_image(prompt, **kwargs)
    else:
        return get_fallback_image()
```

### 📊 **Cost Monitoring**

#### **API Usage Tracking**
- Log all fal.ai API calls with timestamps
- Track costs per feature/session
- Set daily/weekly spending limits

#### **Development Metrics**
- Count API calls per development session
- Monitor which features consume most credits
- Optimize high-usage workflows first

### 🎯 **Current Implementation Status**

#### **✅ Completed:**
- fal.ai Flux Dev integration
- CORS proxy for image serving
- UI component emission to frontend
- WebSocket completion detection with fallback
- Mock image system for development (USE_MOCK_IMAGES=true)
- Development mode configuration (DEVELOPMENT_MODE=true)
- Cost-conscious development workflow implemented

#### **✅ Current Status:**
- Full end-to-end image generation working
- Mock mode enabled by default to save API costs
- Frontend can test UI components without API calls
- Real image generation available when needed (set USE_MOCK_IMAGES=false)

#### **🔄 Optional Future Enhancements:**
1. Image caching system by prompt hash
2. Cost monitoring dashboard
3. Sample image library expansion
4. A/B testing for different mock images

### 💡 **Developer Tips**

#### **For Feature Development:**
- Start with mock images and UI flows
- Test backend logic separately from image generation
- Generate real images only for final demos

#### **For Bug Fixing:**
- Use existing generated images to test fixes
- Avoid regenerating images unless bug is image-specific
- Keep debug images for regression testing

#### **For Performance Testing:**
- Use cached images for load testing
- Test WebSocket efficiency with mock responses
- Measure frontend rendering performance separately

---

*Last Updated: October 2025*
*API Keys: d012f7e7-430f-49c6-b049-fe73de5ec1b4:812610a5058a89d3ae43a083338c229d*