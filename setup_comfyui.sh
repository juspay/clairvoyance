#!/bin/bash

# ==============================================================================
# ComfyUI + fal-API Setup Script for Clairvoyance Voice AI Assistant
# ==============================================================================
# This script automates the setup of ComfyUI with fal-API integration for
# the Clairvoyance voice-activated AI assistant system.
#
# Prerequisites:
# - Python 3.8+ installed
# - Git installed
# - fal.ai API key (get from https://fal.ai/dashboard/keys)
#
# Usage:
#   chmod +x setup_comfyui.sh
#   ./setup_comfyui.sh [INSTALLATION_DIR]
#
# Default installation directory: /Users/$(whoami)/work_dir/temp/ComfyUI
# ==============================================================================

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
DEFAULT_INSTALL_DIR="/Users/$(whoami)/work_dir/temp"
COMFYUI_DIR="${1:-$DEFAULT_INSTALL_DIR}/ComfyUI"
FAL_API_REPO="https://github.com/gokayfem/ComfyUI-fal-API.git"
COMFYUI_REPO="https://github.com/comfyanonymous/ComfyUI.git"

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_header() {
    echo -e "${BLUE}$1${NC}"
    echo "$(printf '=%.0s' {1..60})"
}

# Function to check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Function to get user input with default
get_input() {
    local prompt="$1"
    local default="$2"
    local result

    if [ -n "$default" ]; then
        read -p "$prompt [$default]: " result
        echo "${result:-$default}"
    else
        read -p "$prompt: " result
        echo "$result"
    fi
}

# Function to setup virtual environment
setup_venv() {
    local venv_path="$1"

    print_status "Setting up Python virtual environment..."

    if [ ! -d "$venv_path" ]; then
        python3 -m venv "$venv_path"
        print_success "Virtual environment created at $venv_path"
    else
        print_warning "Virtual environment already exists at $venv_path"
    fi

    # Activate virtual environment
    source "$venv_path/bin/activate"

    # Upgrade pip
    pip install --upgrade pip

    print_success "Virtual environment activated and pip upgraded"
}

# Function to check prerequisites
check_prerequisites() {
    print_header "Checking Prerequisites"

    # Check Python
    if command_exists python3; then
        local python_version=$(python3 --version | cut -d' ' -f2)
        print_success "Python 3 found: $python_version"
    else
        print_error "Python 3 is required but not found. Please install Python 3.8+"
        exit 1
    fi

    # Check Git
    if command_exists git; then
        print_success "Git found"
    else
        print_error "Git is required but not found. Please install Git"
        exit 1
    fi

    # Check pip
    if command_exists pip3 || command_exists pip; then
        print_success "pip found"
    else
        print_error "pip is required but not found. Please install pip"
        exit 1
    fi
}

# Function to install ComfyUI
install_comfyui() {
    print_header "Installing ComfyUI"

    local parent_dir=$(dirname "$COMFYUI_DIR")

    # Create parent directory if it doesn't exist
    mkdir -p "$parent_dir"

    if [ ! -d "$COMFYUI_DIR" ]; then
        print_status "Cloning ComfyUI repository..."
        cd "$parent_dir"
        git clone "$COMFYUI_REPO" ComfyUI
        print_success "ComfyUI repository cloned"
    else
        print_warning "ComfyUI directory already exists. Updating..."
        cd "$COMFYUI_DIR"
        git pull origin master || print_warning "Failed to update ComfyUI (may have local changes)"
    fi

    cd "$COMFYUI_DIR"

    # Setup virtual environment for ComfyUI
    setup_venv "$COMFYUI_DIR/comfyui_venv"

    # Install ComfyUI requirements
    if [ -f "requirements.txt" ]; then
        print_status "Installing ComfyUI requirements..."
        pip install -r requirements.txt
        print_success "ComfyUI requirements installed"
    fi

    # Install PyTorch with CUDA support (adjust based on your system)
    print_status "Installing PyTorch..."
    pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
    print_success "PyTorch installed"
}

# Function to install fal-API custom nodes
install_fal_api() {
    print_header "Installing ComfyUI-fal-API Custom Nodes"

    local custom_nodes_dir="$COMFYUI_DIR/custom_nodes"
    local fal_api_dir="$custom_nodes_dir/ComfyUI-fal-API"

    # Create custom_nodes directory if it doesn't exist
    mkdir -p "$custom_nodes_dir"

    if [ ! -d "$fal_api_dir" ]; then
        print_status "Cloning ComfyUI-fal-API repository..."
        cd "$custom_nodes_dir"
        git clone "$FAL_API_REPO"
        print_success "ComfyUI-fal-API repository cloned"
    else
        print_warning "ComfyUI-fal-API directory already exists. Updating..."
        cd "$fal_api_dir"
        git pull origin main || print_warning "Failed to update ComfyUI-fal-API (may have local changes)"
    fi

    cd "$fal_api_dir"

    # Install fal-API requirements
    if [ -f "requirements.txt" ]; then
        print_status "Installing fal-API requirements..."
        # Activate ComfyUI venv first
        source "$COMFYUI_DIR/comfyui_venv/bin/activate"
        pip install -r requirements.txt
        print_success "fal-API requirements installed"
    fi
}

# Function to configure fal-API
configure_fal_api() {
    print_header "Configuring fal-API"

    local config_file="$COMFYUI_DIR/custom_nodes/ComfyUI-fal-API/config.ini"

    # Get fal.ai API key from user
    echo ""
    echo "You need a fal.ai API key to use the fal-API nodes."
    echo "Get your API key from: https://fal.ai/dashboard/keys"
    echo ""

    local fal_key
    while [ -z "$fal_key" ]; do
        fal_key=$(get_input "Enter your fal.ai API key" "")
        if [ -z "$fal_key" ]; then
            print_warning "fal.ai API key is required. Please enter a valid key."
        fi
    done

    # Update config.ini
    if [ -f "$config_file" ]; then
        print_status "Updating fal-API configuration..."
        sed -i.bak "s/<your_fal_api_key_here>/$fal_key/" "$config_file"
        print_success "fal-API configuration updated"
    else
        print_status "Creating fal-API configuration..."
        cat > "$config_file" << EOF
[API]
FAL_KEY = $fal_key
EOF
        print_success "fal-API configuration created"
    fi

    # Also set environment variable for current session
    export FAL_KEY="$fal_key"

    print_status "fal.ai API key configured successfully"
}

# Function to update Clairvoyance environment
update_clairvoyance_env() {
    print_header "Updating Clairvoyance Configuration"

    local env_file="/Users/$(whoami)/work_dir/temp/clairvoyance/.env"
    local env_example_file="/Users/$(whoami)/work_dir/temp/clairvoyance/.env.example"

    print_status "Updating ComfyUI configuration in Clairvoyance..."

    # ComfyUI configuration values
    local comfyui_base_url="http://localhost:8188"
    local comfyui_ws_url="ws://localhost:8188/ws"

    # Function to update or add environment variable
    update_env_var() {
        local file="$1"
        local var_name="$2"
        local var_value="$3"

        if [ -f "$file" ]; then
            if grep -q "^${var_name}=" "$file"; then
                # Update existing variable
                sed -i.bak "s|^${var_name}=.*|${var_name}=${var_value}|" "$file"
            else
                # Add new variable
                echo "${var_name}=${var_value}" >> "$file"
            fi
        else
            # Create file with variable
            echo "${var_name}=${var_value}" > "$file"
        fi
    }

    # Update .env file if it exists
    if [ -f "$env_file" ]; then
        update_env_var "$env_file" "ENABLE_COMFYUI" "true"
        update_env_var "$env_file" "COMFYUI_BASE_URL" "$comfyui_base_url"
        update_env_var "$env_file" "COMFYUI_WEBSOCKET_URL" "$comfyui_ws_url"
        update_env_var "$env_file" "COMFYUI_TIMEOUT" "300"
        print_success "Updated $env_file"
    fi

    # Update .env.example file if it exists
    if [ -f "$env_example_file" ]; then
        update_env_var "$env_example_file" "ENABLE_COMFYUI" "true"
        update_env_var "$env_example_file" "COMFYUI_BASE_URL" "$comfyui_base_url"
        update_env_var "$env_example_file" "COMFYUI_WEBSOCKET_URL" "$comfyui_ws_url"
        update_env_var "$env_example_file" "COMFYUI_TIMEOUT" "300"
        print_success "Updated $env_example_file"
    fi

    print_success "Clairvoyance configuration updated"
}

# Function to create startup scripts
create_startup_scripts() {
    print_header "Creating Startup Scripts"

    # Create ComfyUI startup script
    local startup_script="$COMFYUI_DIR/start_comfyui.sh"

    cat > "$startup_script" << 'EOF'
#!/bin/bash

# ComfyUI Startup Script for Clairvoyance
# Auto-generated by setup_comfyui.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PATH="$SCRIPT_DIR/comfyui_venv"

echo "Starting ComfyUI for Clairvoyance..."
echo "Virtual environment: $VENV_PATH"
echo "ComfyUI directory: $SCRIPT_DIR"

# Activate virtual environment
if [ -f "$VENV_PATH/bin/activate" ]; then
    source "$VENV_PATH/bin/activate"
    echo "✅ Virtual environment activated"
else
    echo "❌ Virtual environment not found at $VENV_PATH"
    echo "Please run the setup script again."
    exit 1
fi

# Check if fal-API is configured
CONFIG_FILE="$SCRIPT_DIR/custom_nodes/ComfyUI-fal-API/config.ini"
if [ -f "$CONFIG_FILE" ] && grep -q "FAL_KEY.*=" "$CONFIG_FILE" && ! grep -q "<your_fal_api_key_here>" "$CONFIG_FILE"; then
    echo "✅ fal-API configuration found"
else
    echo "⚠️  fal-API not configured properly"
    echo "Please check $CONFIG_FILE and ensure your fal.ai API key is set"
fi

# Start ComfyUI
echo "🚀 Starting ComfyUI server..."
echo "Access ComfyUI at: http://localhost:8188"
echo "Press Ctrl+C to stop"

cd "$SCRIPT_DIR"
python main.py --listen 0.0.0.0 --port 8188
EOF

    chmod +x "$startup_script"
    print_success "ComfyUI startup script created: $startup_script"

    # Create a system service script (optional)
    local service_script="$COMFYUI_DIR/install_service.sh"

    cat > "$service_script" << 'EOF'
#!/bin/bash

# Install ComfyUI as a system service (macOS/Linux)
# This is optional and requires sudo privileges

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="comfyui"

echo "Installing ComfyUI as a system service..."
echo "This requires sudo privileges."

# Detect OS
if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS - create LaunchAgent
    PLIST_FILE="$HOME/Library/LaunchAgents/com.clairvoyance.comfyui.plist"

    cat > "$PLIST_FILE" << EOL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.clairvoyance.comfyui</string>
    <key>ProgramArguments</key>
    <array>
        <string>${SCRIPT_DIR}/start_comfyui.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>StandardOutPath</key>
    <string>${SCRIPT_DIR}/comfyui.log</string>
    <key>StandardErrorPath</key>
    <string>${SCRIPT_DIR}/comfyui_error.log</string>
</dict>
</plist>
EOL

    # Load the service
    launchctl load "$PLIST_FILE"
    echo "✅ ComfyUI service installed and started"
    echo "Use 'launchctl unload $PLIST_FILE' to stop"

elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    # Linux - create systemd service
    SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

    sudo tee "$SERVICE_FILE" > /dev/null << EOL
[Unit]
Description=ComfyUI for Clairvoyance
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${SCRIPT_DIR}/start_comfyui.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOL

    # Enable and start the service
    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME"
    sudo systemctl start "$SERVICE_NAME"

    echo "✅ ComfyUI service installed and started"
    echo "Use 'sudo systemctl stop $SERVICE_NAME' to stop"
    echo "Use 'sudo systemctl status $SERVICE_NAME' to check status"

else
    echo "❌ Unsupported OS for service installation"
    echo "Please start ComfyUI manually using: $SCRIPT_DIR/start_comfyui.sh"
fi
EOF

    chmod +x "$service_script"
    print_success "Service installation script created: $service_script"
}

# Function to test installation
test_installation() {
    print_header "Testing Installation"

    # Test Python imports
    print_status "Testing Python imports..."

    cd "$COMFYUI_DIR"
    source "comfyui_venv/bin/activate"

    # Test basic imports
    python3 -c "import torch; print('✅ PyTorch:', torch.__version__)" || print_error "❌ PyTorch import failed"
    python3 -c "import fal_client; print('✅ fal-client installed')" || print_error "❌ fal-client import failed"

    # Test ComfyUI nodes
    if [ -d "custom_nodes/ComfyUI-fal-API/nodes" ]; then
        print_success "✅ fal-API nodes directory found"
    else
        print_error "❌ fal-API nodes directory not found"
    fi

    # Check configuration
    local config_file="custom_nodes/ComfyUI-fal-API/config.ini"
    if [ -f "$config_file" ] && ! grep -q "<your_fal_api_key_here>" "$config_file"; then
        print_success "✅ fal-API configuration appears valid"
    else
        print_warning "⚠️  fal-API configuration needs to be checked"
    fi

    print_success "Installation test completed"
}

# Function to display post-installation instructions
display_instructions() {
    print_header "Installation Complete!"

    cat << EOF

🎉 ComfyUI with fal-API integration has been successfully set up!

📍 Installation Location: $COMFYUI_DIR

🚀 To start ComfyUI:
   cd $COMFYUI_DIR
   ./start_comfyui.sh

🌐 ComfyUI Web Interface: http://localhost:8188

📋 Available fal-API Nodes:
   • Flux Pro/Dev/Schnell (Text-to-Image)
   • Flux Pro Kontext (Image-to-Image)
   • QwenImageEdit, SeedEdit V3 (Image Editing)
   • Recraft V3, Sana, Ideogram v3
   • And many more...

⚙️  Configuration Files:
   • ComfyUI config: $COMFYUI_DIR/custom_nodes/ComfyUI-fal-API/config.ini
   • Clairvoyance: /Users/$(whoami)/work_dir/temp/clairvoyance/.env

🔧 To install as system service (optional):
   cd $COMFYUI_DIR
   ./install_service.sh

📚 Documentation:
   • ComfyUI: https://github.com/comfyanonymous/ComfyUI
   • fal-API: https://github.com/gokayfem/ComfyUI-fal-API
   • fal.ai docs: https://fal.ai/docs

⚠️  Important Notes:
   1. Make sure your fal.ai API key is correctly set in config.ini
   2. ComfyUI needs to be running for Clairvoyance image generation
   3. The first run may take longer as models are downloaded

💡 Troubleshooting:
   • Check logs: $COMFYUI_DIR/comfyui.log
   • Restart ComfyUI if you encounter issues
   • Ensure sufficient disk space for model downloads

EOF

    print_success "Setup completed successfully! 🎉"
}

# Main installation function
main() {
    print_header "ComfyUI + fal-API Setup for Clairvoyance"

    echo "This script will install and configure ComfyUI with fal-API integration"
    echo "for the Clairvoyance voice-activated AI assistant."
    echo ""
    echo "Installation directory: $COMFYUI_DIR"
    echo ""

    read -p "Continue with installation? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_warning "Installation cancelled by user"
        exit 0
    fi

    # Run installation steps
    check_prerequisites
    install_comfyui
    install_fal_api
    configure_fal_api
    update_clairvoyance_env
    create_startup_scripts
    test_installation
    display_instructions
}

# Run main function
main "$@"