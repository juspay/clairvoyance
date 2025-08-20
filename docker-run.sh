#!/bin/bash

# Docker run script for clairvoyance voice application
# This script helps you easily start the application with PostgreSQL

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if Docker is running
check_docker() {
    if ! docker info > /dev/null 2>&1; then
        log_error "Docker is not running. Please start Docker Desktop and try again."
        exit 1
    fi
    log_success "Docker is running"
}

# Check if .env file exists
check_env_file() {
    if [ ! -f ".env" ]; then
        log_warning ".env file not found. Creating from .env.example..."
        if [ -f ".env.example" ]; then
            cp .env.example .env
            log_info "Please edit .env file with your API keys before running the application"
            log_info "Required keys: DAILY_API_KEY, GEMINI_API_KEY, AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, GOOGLE_CREDENTIALS_JSON"
        else
            log_error ".env.example file not found. Please create .env file manually."
            exit 1
        fi
    else
        log_success ".env file found"
    fi
}

# Function to start services
start_services() {
    log_info "Starting PostgreSQL and Redis services..."
    docker-compose up -d postgres redis
    
    log_info "Waiting for services to be healthy..."
    timeout=60
    elapsed=0
    
    while [ $elapsed -lt $timeout ]; do
        if docker-compose exec postgres pg_isready -U clairvoyance_user -d clairvoyance > /dev/null 2>&1; then
            log_success "PostgreSQL is ready"
            break
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    
    if [ $elapsed -ge $timeout ]; then
        log_error "PostgreSQL failed to start within $timeout seconds"
        exit 1
    fi
}

# Function to build and start the application
start_app() {
    log_info "Building and starting the application..."
    docker-compose up --build -d app
    log_success "Application started successfully"
}

# Function to show logs
show_logs() {
    log_info "Showing application logs (press Ctrl+C to exit)..."
    docker-compose logs -f app
}

# Function to stop services
stop_services() {
    log_info "Stopping all services..."
    docker-compose down
    log_success "Services stopped"
}

# Function to reset database
reset_database() {
    log_warning "This will delete all data in the database!"
    read -p "Are you sure you want to continue? (y/N): " confirm
    if [[ $confirm =~ ^[Yy]$ ]]; then
        log_info "Stopping services and removing volumes..."
        docker-compose down -v
        log_info "Restarting services..."
        start_services
        start_app
        log_success "Database reset complete"
    else
        log_info "Database reset cancelled"
    fi
}

# Main script
case "${1:-start}" in
    "start")
        log_info "Starting clairvoyance voice application..."
        check_docker
        check_env_file
        start_services
        start_app
        echo ""
        log_success "🚀 Clairvoyance is running!"
        log_info "Application: http://localhost:8000"
        log_info "Health check: http://localhost:8000/health"
        log_info "Database: localhost:5432 (user: clairvoyance_user, db: clairvoyance)"
        echo ""
        log_info "To view logs: ./docker-run.sh logs"
        log_info "To stop: ./docker-run.sh stop"
        ;;
    "stop")
        stop_services
        ;;
    "logs")
        show_logs
        ;;
    "restart")
        stop_services
        sleep 2
        exec "$0" start
        ;;
    "reset-db")
        reset_database
        ;;
    "status")
        log_info "Service status:"
        docker-compose ps
        ;;
    "shell")
        log_info "Opening shell in application container..."
        docker-compose exec app /bin/bash
        ;;
    "psql")
        log_info "Opening PostgreSQL shell..."
        docker-compose exec postgres psql -U clairvoyance_user -d clairvoyance
        ;;
    "help"|"-h"|"--help")
        echo "Usage: $0 [command]"
        echo ""
        echo "Commands:"
        echo "  start      Start all services (default)"
        echo "  stop       Stop all services"
        echo "  restart    Restart all services"
        echo "  logs       Show application logs"
        echo "  status     Show service status"
        echo "  reset-db   Reset database (removes all data)"
        echo "  shell      Open shell in app container"
        echo "  psql       Open PostgreSQL shell"
        echo "  help       Show this help message"
        ;;
    *)
        log_error "Unknown command: $1"
        log_info "Use '$0 help' to see available commands"
        exit 1
        ;;
esac