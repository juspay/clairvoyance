# Project Brief: Clairvoyance Voice Agent

## 1. Core Objective

The primary goal of the Clairvoyance project is to develop a sophisticated, real-time, conversational voice AI agent. This agent is designed to integrate with various backend services and platforms (e.g., e-commerce shops) to provide seamless, automated assistance to users.

## 2. Key Components

- **Voice Agent:** The core component, built using the PipeCat framework, that handles real-time audio input/output, speech-to-text (STT), and text-to-speech (TTS).
- **LLM Integration:** Leverages Large Language Models (LLMs) like Azure OpenAI to understand user intent, process information, and generate conversational responses.
- **Tooling System:** A critical feature that allows the LLM to execute actions and retrieve information from external systems. This is managed via the Model Context Protocol (MCP).

## 3. Scope and Functionality

- **Real-time Conversation:** Engage in natural, low-latency voice conversations.
- **Dynamic Tool Use:** Connect to and utilize tools from a remote MCP server, allowing for flexible and extensible functionality without redeploying the agent.
- **Context-Awareness:** Maintain session-specific context (e.g., user ID, shop details, session tokens) to perform authenticated and personalized actions.
- **Secure Integration:** Ensure that sensitive data, such as authentication tokens and user context, is handled securely and not directly exposed to the LLM.
- **Modes of Operation:** Support different operational modes, such as `LIVE` for production use and `TEST` for development and debugging.
