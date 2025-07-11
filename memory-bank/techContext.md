# Tech Context: Clairvoyance Voice Agent

## 1. Core Frameworks and Libraries

- **Python:** The primary programming language for the application backend.
- **PipeCat:** The core framework for building real-time voice and video AI applications. It provides the pipeline structure, audio/video processing, and service integrations.
- **FastAPI:** (Implicitly used by PipeCat for web-facing components) A modern, high-performance web framework for building APIs.
- **Loguru:** Used for logging, providing a more powerful and flexible alternative to the standard library.
- **Dotenv:** Manages environment variables for configuration.

## 2. Key Services and Integrations

- **Daily:** The transport layer service used for handling real-time audio/video communication and managing rooms.
- **Google STT (Speech-to-Text):** The service used to transcribe user audio into text.
- **Azure OpenAI:** The LLM provider used for natural language understanding, conversational logic, and function calling.
- **TTS Services:** The agent is designed to be flexible with Text-to-Speech providers, with specific implementations for services like Google TTS.

## 3. Tooling and Protocol

- **Model Context Protocol (MCP):** The specification used for the external tooling system. The agent communicates with a remote server that adheres to this protocol.
- **JSON-RPC 2.0:** The transport protocol used for MCP communication, specifically over a streaming HTTP connection.
- **HTTPX:** The asynchronous HTTP client used to communicate with the remote MCP server.

## 4. Development and Environment

- **Vercel AI SDK:** The remote MCP server is built using the Vercel AI SDK, which provides tools for creating AI-powered applications and endpoints.
- **Command-Line Interface (CLI):** The agent is launched via a Python script that accepts command-line arguments (`argparse`) for configuration, including session details, tokens, and feature flags.
- **OpenTelemetry & Langfuse:** Used for tracing and observability, providing insights into the agent's performance and behavior during a session.
