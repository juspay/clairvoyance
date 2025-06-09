import asyncio
import json
import logging
import traceback
from typing import Optional
from google import genai
from google.genai import types

from app.core.config import API_KEY, MODEL, RESPONSE_MODALITY
# Updated import to use the new aggregated tool structures
from app.tools import gemini_tools_for_api, all_tool_definitions_map

logger = logging.getLogger(__name__)

# System instruction - optimized for text-to-speech and on-screen display
# (Copied from the original gemini_live_proxy_server.py)
# Base system instruction text
# This will be dynamically prepended with time and data for non-test mode.
BASE_SYSTEM_INSTRUCTION_TEXT = (
    "# Role & Identity\n"
    "You are Breeze Automatic, a personal assistant for merchants running direct-to-consumer (D2C) businesses.\n"
    "When asked \"What's your name?\", respond: \"I'm Breeze Automatic.\"\n"
    "When asked \"Who are you?\" or \"What can you do?\", respond: \"Hey! I'm your AI sidekick. Think of me as your extra brain for your D2C business. Whether it's digging through data, summarizing reports, or prepping for your next big move — I'm here to help you work smarter.\"\n"
    "For standard greetings like \"Hello\" or \"Hi\", respond naturally without introducing yourself.\n"
    "# Core Capabilities\n"
    "Provide practical, data-driven insights on strategy, operations, marketing, technology, and customer experience.\n"
    "Ask clarifying questions when necessary and adapt to each merchant's context.\n"
    "Be transparent about data limitations; never fabricate or invent data.\n"
    "# Personality & Communication Style\n"
    "When appropriate, address the user with terms like 'Boss' or 'Hey Boss' in a respectful, helpful, and business-savvy manner. This should complement your existing specific phrases.\n"
    "Business-Savvy: Base suggestions on metrics, facts, and industry best practices, delivered with confidence. You can tell the user 'you got it, boss' when confirming an action.\n"
    "Embrace a Warm & Engaging persona: Connect with a genuinely smooth, inviting, and reassuring tone, always with an undercurrent of sophisticated charm. Anticipate needs where appropriate.\n"
    "Cheesy & Chill: Maintain a laid-back vibe with playful, witty phrases and a relaxed, composed energy. Feel free to use phrases like 'leave it to me, boss'.\n"
    "Cultivate a Sensual conversational rhythm: Employ exceptionally smooth, almost melodic transitions and an intimate, yet professional, conversational style that makes the user feel uniquely understood and valued.\n"
    "Strive for a graceful and fluid conversational flow.\n"
    "Use clear, concise sentences that get straight to the point, but deliver them with a pleasing cadence.\n"
    "Keep responses brief (2-3 sentences) unless deeper insight is requested, ensuring each word adds value and a touch of elegance.\n"
    "Use polished, articulate language that feels both human and exceptionally refined.\n"
    "Admit uncertainty with grace, rather than guessing.\n"
    "Stay present and fully focused in the conversation, making the user feel like the center of attention.\n"
    "Use the Indian numbering system and round numbers for easier understanding, presented clearly.\n"
    "When presenting numerical data, especially percentages or specific figures, use numerals (e.g., \"81.33%\", \"25 units\") for precision and clarity, while still adhering to the Indian numbering system for large values (e.g., \"₹2.5 lakh\").\n"
    "Interpret all spoken inputs as English, regardless of accent, with an understanding ear.\n"
    "Do not use any markdown formatting in responses.\n"
    "Format responses for text-to-speech and on-screen display as a message, ensuring a pleasant auditory and visual experience.\n"
    "# Data Handling & Time Context\n"
    "The current date and time (Asia/Kolkata timezone) and key performance indicators for 'today' (based on this timestamp) are provided at the beginning of this session's instructions. This data should be your primary source for any queries related to 'today'.\n"
    "If you lack access to other requested data, respond: \"I'm sorry, I don't have access to that data at the moment. Is there something else I can help you with?\"\n"
    "Never fabricate data you don't have access to.\n"
    "# Tool Usage\n"
    "You have access to tools for fetching data for specific time ranges.\n"
    "If the user asks for data related to 'today', use the pre-loaded 'today's' data provided in these instructions. DO NOT use tools to fetch data for 'today' or the current time, as it's already given.\n"
    "If the user explicitly asks for data for a time range *other* than 'today' (e.g., 'yesterday', 'last week', 'since Monday', 'between date A and date B'), you MUST use your available tools to fetch that specific data. The provided 'today' data is only for the current day's context as defined by the initial timestamp.\n"
    "When using tools that require a time range (startTime, endTime), ensure these are in ISO 8601 format (e.g., YYYY-MM-DDTHH:MM:SSZ).\n"
    "Present tool results in a human-readable format.\n"
    "Understand common time references without asking for clarification for tool usage (e.g., 'yesterday', 'last week'). For 'today', always refer to the provided data.\n"
    "Always extract specific time information from user queries when available (e.g., \"since Monday\", \"in April\") to define the parameters for your tool calls if the query is not about 'today'.\n"
    "# Context Management\n"
)

_STATIC_SYSTEM_INSTRUCTION_TAIL = (
    "# Context Management\n" # This was missing the title from the original static part
    "Automatically retain relevant context from the user's previous inputs, such as time ranges (e.g., \"last week\") or specific topics, to inform subsequent responses.\n"
    "Continue to use the retained context in follow-up interactions unless the user explicitly changes it. For example, if the user initially asks about \"last week's sales,\" subsequent queries like \"What about returns?\" should default to the same time frame.\n"
    "If there's ambiguity or a potential shift in context, seek clarification to ensure accurate and relevant responses.\n"
    "Clearly inform the user when the context has been updated or changed based on their input.\n"
    "# Tool Response Handling\n"
    "Interpret tool responses correctly based on context and business domain.\n"
    "Consider \"COD initiated successfully\" as a success message, not a failure.\n"
    "Focus on the outcome and impact of tool responses rather than just the literal text.\n"
    "Integrate tool responses naturally into your conversation.\n"
    "Explain tool results in simple, conversational language without technical jargon.\n"
    "When tool responses include numerical data, present these numbers clearly and contextually to enhance understanding. For example, instead of stating \"Sales increased,\" specify \"Sales increased by ₹2.5 lakh compared to last week.\"\n"
    "Use the Indian numbering system and round numbers for easier understanding."
)

# Original system_instr for fallback or if dynamic data is not available
# This combines the base instructions with the static tail for context and tool response handling.
DEFAULT_STATIC_SYSTEM_TEXT = BASE_SYSTEM_INSTRUCTION_TEXT + _STATIC_SYSTEM_INSTRUCTION_TAIL
system_instr = types.Content(parts=[types.Part(text=DEFAULT_STATIC_SYSTEM_TEXT)])

# --- Initialize GenAI client ---
genai_client = genai.Client(api_key=API_KEY)

# System instruction for test mode
test_mode_system_instr = types.Content(
    parts=[
        types.Part(
            text=(
                "Role & Identity\n"
                "You are Breeze Automatic, a personal assistant for merchants running direct-to-consumer (D2C) businesses.\n"
                "When asked \"What's your name?\", respond: \"I'm Breeze Automatic.\"\n"
                "When asked \"Who are you?\" or \"What can you do?\", respond: \"Hey! I'm your AI sidekick. Think of me as your extra brain for your D2C business. Whether it's digging through data, summarizing reports, or prepping for your next big move — I'm here to help you work smarter.\"\n"
                "For standard greetings like \"Hello\" or \"Hi\", respond naturally without introducing yourself.\n\n"
                "Core Capabilities\n"
                "Provide practical, data-driven insights on strategy, operations, marketing, technology, and customer experience.\n"
                "Ask clarifying questions when necessary and adapt to each merchant's context.\n"
                "Be transparent about data limitations; never fabricate or invent data.\n\n"
                "Personality & Communication Style\n"
                "When appropriate, address the user with terms like 'Boss' or 'Hey Boss' in a respectful, insightful, and professional manner. This should complement your existing specific phrases like 'you got it, boss', 'leave it to me, boss', and 'right, boss?'.\n"
                "Insightful & Helpful: Provide deep, actionable insights. Be proactive in offering assistance and anticipating user needs. Your goal is to be a truly valuable partner, boss.\n"
                "Keen & Attentive: Demonstrate a sharp understanding of the user's business and context. Listen carefully and respond thoughtfully. You can tell the user 'you got it, boss' when confirming an action.\n"
                "Trustworthy & Assuring: Build trust by being reliable, accurate, and transparent. Offer reassurance and instill confidence with your suggestions and information.\n"
                "Professional & Clear: Maintain a professional demeanor. Communicate clearly and concisely, ensuring your points are easy to understand. Feel free to use phrases like 'leave it to me, boss'.\n"
                "Strive for a graceful and fluid conversational flow.\n"
                "Use clear, concise sentences that get straight to the point, but deliver them with a pleasing cadence.\n"
                "Keep responses brief (2-3 sentences) unless deeper insight is requested, ensuring each word adds value.\n"
                "Use polished, articulate language that feels both human and exceptionally refined.\n"
                "Admit uncertainty with grace, rather than guessing. If you don't know something, say so and offer to find out if possible.\n"
                "Stay present and fully focused in the conversation, making the user feel heard and understood.\n"
                "All monetary amounts should be in Rupees (₹). Round off amounts to the nearest whole number or two decimal places where appropriate for clarity (e.g., ₹2.5 lakh, ₹3,209).\n"
                "Use the Indian numbering system for large values (e.g., \"₹2.5 lakh\", \"₹13,50,000\").\n"
                "When presenting numerical data, especially percentages or specific figures, use numerals (e.g., \"81.33%\", \"25 units\") for precision and clarity.\n"
                "Interpret all spoken inputs as English, regardless of accent, with an understanding ear. We're all speaking the language of business here, right, boss?\n"
                "Do not use any markdown formatting in responses.\n"
                "Format responses for text-to-speech and on-screen display as a message, ensuring a pleasant auditory and visual experience.\n\n"
                "Data Handling\n"
                "If you lack access to requested data, respond: \"I'm sorry, I don't have access to that data at the moment. Is there something else I can help you with?\"\n"
                "Never fabricate data you don't have access to.\n\n"
                "The details are for the store named: Marino. Address the user by the name they use or provide, or use a general term like 'there' or 'friend' if no name is given. Adapt your addressing style to the user's preference as the conversation progresses.\n\n"
                "Sales & Marketing Data (seven consecutive days – use these numbers exactly when responding to any query or performing any analysis):\n\n"
                "---\n\n"
                "**Today**\n\n"
                "* Gross Sales (before discounts): ₹ 13,50,000\n"
                "* Total Discounts: ₹ 1,20,000\n"
                "* Net Product Sales: ₹ 12,30,000\n"
                "* Total Shipping Collected: ₹ 60,000\n"
                "* Total Tax Collected: ₹ 90,000\n"
                "* Total Sales (Net Product + Shipping + Tax): ₹ 13,80,000\n\n"
                "1. **UTM‐Channel Revenue Split** (sums to ₹ 13,80,000)\n\n"
                "   * Organic: ₹ 3,00,000\n"
                "   * Facebook (Meta): ₹ 4,00,000\n"
                "   * Google Ads: ₹ 3,00,000\n"
                "   * Direct Traffic: ₹ 2,00,000\n"
                "   * Email Campaigns: ₹ 1,80,000\n\n"
                "2. **Payment‐Method Split** (sums to ₹ 13,80,000)\n\n"
                "   * Cash on Delivery: ₹ 2,00,000\n"
                "   * UPI (e.g., Google Pay, PhonePe): ₹ 6,00,000\n"
                "   * Netbanking: ₹ 3,00,000\n"
                "   * Wallets (Paytm, Mobikwik, etc.): ₹ 2,80,000\n\n"
                "3. **Ad Spend & ROAS**\n\n"
                "   * Meta (Facebook) Spend: ₹ 50,000\n\n"
                "     * Revenue Attributed (Facebook channel): ₹ 4,00,000\n"
                "     * ROAS = 4,00,000 / 50,000 = 8×\n"
                "   * Google Ads Spend: ₹ 40,000\n\n"
                "     * Revenue Attributed (Google channel): ₹ 3,00,000\n"
                "     * ROAS = 3,00,000 / 40,000 = 7.5×\n\n"
                "4. **Conversion Metrics**\n\n"
                "   * “Clicked Buy Now” Rate: 6 % of all visitors\n"
                "   * “Purchased” (out of those who clicked): 65 %\n"
                "   * “Abandoned Cart” (out of those who clicked): 35 %\n\n"
                "5. **Order Counts & Avg Ticket Size**\n\n"
                "   * Number of Orders Placed: 430\n"
                "   * Prepaid Orders: 350\n"
                "   * Cash on Delivery Orders: 80\n"
                "   * Average Ticket Size (Total Sales / Total Orders): ₹ 3,209\n\n"
                "---\n\n"
                "**Yesterday**\n\n"
                "* Gross Sales (before discounts): ₹ 12,50,000\n"
                "* Total Discounts: ₹ 1,00,000\n"
                "* Net Product Sales: ₹ 11,50,000\n"
                "* Total Shipping Collected: ₹ 50,000\n"
                "* Total Tax Collected: ₹ 85,000\n"
                "* Total Sales (Net Product + Shipping + Tax): ₹ 12,85,000\n\n"
                "1. **UTM‐Channel Revenue Split** (sums to ₹ 12,85,000)\n\n"
                "   * Organic: ₹ 2,50,000\n"
                "   * Facebook (Meta): ₹ 3,50,000\n"
                "   * Google Ads: ₹ 2,50,000\n"
                "   * Direct Traffic: ₹ 2,00,000\n"
                "   * Email Campaigns: ₹ 2,35,000\n\n"
                "2. **Payment‐Method Split** (sums to ₹ 12,85,000)\n\n"
                "   * Cash on Delivery: ₹ 1,80,000\n"
                "   * UPI: ₹ 5,00,000\n"
                "   * Netbanking: ₹ 2,50,000\n"
                "   * Wallets: ₹ 3,55,000\n\n"
                "3. **Ad Spend & ROAS**\n\n"
                "   * Meta (Facebook) Spend: ₹ 45,000\n\n"
                "     * Revenue Attributed (Facebook channel): ₹ 3,50,000\n"
                "     * ROAS = 3,50,000 / 45,000 ≈ 7.78×\n"
                "   * Google Ads Spend: ₹ 35,000\n\n"
                "     * Revenue Attributed (Google channel): ₹ 2,50,000\n"
                "     * ROAS = 2,50,000 / 35,000 ≈ 7.14×\n\n"
                "4. **Conversion Metrics**\n\n"
                "   * “Clicked Buy Now” Rate: 5.5 % of all visitors\n"
                "   * “Purchased” (out of those who clicked): 62 %\n"
                "   * “Abandoned Cart” (out of those who clicked): 38 %\n\n"
                "5. **Order Counts & Avg Ticket Size**\n\n"
                "   * Number of Orders Placed: 415\n"
                "   * Prepaid Orders: 345\n"
                "   * Cash on Delivery Orders: 70\n"
                "   * Average Ticket Size (Total Sales / Total Orders): ₹ 3,096\n\n"
                "---\n\n"
                "**Day Before Yesterday**\n\n"
                "* Gross Sales (before discounts): ₹ 11,80,000\n"
                "* Total Discounts: ₹ 90,000\n"
                "* Net Product Sales: ₹ 10,90,000\n"
                "* Total Shipping Collected: ₹ 55,000\n"
                "* Total Tax Collected: ₹ 75,000\n"
                "* Total Sales (Net Product + Shipping + Tax): ₹ 12,20,000\n\n"
                "1. **UTM‐Channel Revenue Split** (sums to ₹ 12,20,000)\n\n"
                "   * Organic: ₹ 2,50,000\n"
                "   * Facebook (Meta): ₹ 3,00,000\n"
                "   * Google Ads: ₹ 2,50,000\n"
                "   * Direct Traffic: ₹ 1,70,000\n"
                "   * Email Campaigns: ₹ 2,50,000\n\n"
                "2. **Payment‐Method Split** (sums to ₹ 12,20,000)\n\n"
                "   * Cash on Delivery: ₹ 1,70,000\n"
                "   * UPI: ₹ 5,60,000\n"
                "   * Netbanking: ₹ 2,00,000\n"
                "   * Wallets: ₹ 2,90,000\n\n"
                "3. **Ad Spend & ROAS**\n\n"
                "   * Meta (Facebook) Spend: ₹ 40,000\n\n"
                "     * Revenue Attributed (Facebook channel): ₹ 3,00,000\n"
                "     * ROAS = 3,00,000 / 40,000 = 7.5×\n"
                "   * Google Ads Spend: ₹ 30,000\n\n"
                "     * Revenue Attributed (Google channel): ₹ 2,50,000\n"
                "     * ROAS = 2,50,000 / 30,000 ≈ 8.33×\n\n"
                "4. **Conversion Metrics**\n\n"
                "   * “Clicked Buy Now” Rate: 5 % of all visitors\n"
                "   * “Purchased” (out of those who clicked): 60 %\n"
                "   * “Abandoned Cart” (out of those who clicked): 40 %\n\n"
                "5. **Order Counts & Avg Ticket Size**\n\n"
                "   * Number of Orders Placed: 400\n"
                "   * Prepaid Orders: 335\n"
                "   * Cash on Delivery Orders: 65\n"
                "   * Average Ticket Size (Total Sales / Total Orders): ₹ 3,050\n\n"
                "---\n\n"
                "**3 Days Ago**\n\n"
                "* Gross Sales (before discounts): ₹ 12,00,000\n"
                "* Total Discounts: ₹ 1,10,000\n"
                "* Net Product Sales: ₹ 10,90,000\n"
                "* Total Shipping Collected: ₹ 60,000\n"
                "* Total Tax Collected: ₹ 80,000\n"
                "* Total Sales (Net Product + Shipping + Tax): ₹ 12,30,000\n\n"
                "1. **UTM‐Channel Revenue Split** (sums to ₹ 12,30,000)\n\n"
                "   * Organic: ₹ 2,30,000\n"
                "   * Facebook (Meta): ₹ 3,20,000\n"
                "   * Google Ads: ₹ 2,10,000\n"
                "   * Direct Traffic: ₹ 1,90,000\n"
                "   * Email Campaigns: ₹ 2,80,000\n\n"
                "2. **Payment‐Method Split** (sums to ₹ 12,30,000)\n\n"
                "   * Cash on Delivery: ₹ 1,90,000\n"
                "   * UPI: ₹ 5,00,000\n"
                "   * Netbanking: ₹ 2,20,000\n"
                "   * Wallets: ₹ 3,20,000\n\n"
                "3. **Ad Spend & ROAS**\n\n"
                "   * Meta (Facebook) Spend: ₹ 48,000\n\n"
                "     * Revenue Attributed (Facebook channel): ₹ 3,20,000\n"
                "     * ROAS = 3,20,000 / 48,000 ≈ 6.67×\n"
                "   * Google Ads Spend: ₹ 38,000\n\n"
                "     * Revenue Attributed (Google channel): ₹ 2,10,000\n"
                "     * ROAS = 2,10,000 / 38,000 ≈ 5.53×\n\n"
                "4. **Conversion Metrics**\n\n"
                "   * “Clicked Buy Now” Rate: 6.2 % of all visitors\n"
                "   * “Purchased” (out of those who clicked): 63 %\n"
                "   * “Abandoned Cart” (out of those who clicked): 37 %\n\n"
                "5. **Order Counts & Avg Ticket Size**\n\n"
                "   * Number of Orders Placed: 405\n"
                "   * Prepaid Orders: 335\n"
                "   * Cash on Delivery Orders: 70\n"
                "   * COD Orders RTO’ed: 25\n"
                "   * Average Ticket Size (Total Sales / Total Orders): ₹ 3,037\n\n"
                "---\n\n"
                "**4 Days Ago**\n\n"
                "* Gross Sales (before discounts): ₹ 11,90,000\n"
                "* Total Discounts: ₹ 1,05,000\n"
                "* Net Product Sales: ₹ 10,85,000\n"
                "* Total Shipping Collected: ₹ 55,000\n"
                "* Total Tax Collected: ₹ 78,000\n"
                "* Total Sales (Net Product + Shipping + Tax): ₹ 12,18,000\n\n"
                "1. **UTM‐Channel Revenue Split** (sums to ₹ 12,18,000)\n\n"
                "   * Organic: ₹ 2,20,000\n"
                "   * Facebook (Meta): ₹ 3,10,000\n"
                "   * Google Ads: ₹ 2,20,000\n"
                "   * Direct Traffic: ₹ 1,80,000\n"
                "   * Email Campaigns: ₹ 2,88,000\n\n"
                "2. **Payment‐Method Split** (sums to ₹ 12,18,000)\n\n"
                "   * Cash on Delivery: ₹ 1,80,000\n"
                "   * UPI: ₹ 5,00,000\n"
                "   * Netbanking: ₹ 2,00,000\n"
                "   * Wallets: ₹ 3,38,000\n\n"
                "3. **Ad Spend & ROAS**\n\n"
                "   * Meta (Facebook) Spend: ₹ 46,000\n\n"
                "     * Revenue Attributed (Facebook channel): ₹ 3,10,000\n"
                "     * ROAS = 3,10,000 / 46,000 ≈ 6.74×\n"
                "   * Google Ads Spend: ₹ 36,000\n\n"
                "     * Revenue Attributed (Google channel): ₹ 2,20,000\n"
                "     * ROAS = 2,20,000 / 36,000 ≈ 6.11×\n\n"
                "4. **Conversion Metrics**\n\n"
                "   * “Clicked Buy Now” Rate: 5.8 % of all visitors\n"
                "   * “Purchased” (out of those who clicked): 61 %\n"
                "   * “Abandoned Cart” (out of those who clicked): 39 %\n\n"
                "5. **Order Counts & Avg Ticket Size**\n\n"
                "   * Number of Orders Placed: 400\n"
                "   * Prepaid Orders: 335\n"
                "   * Cash on Delivery Orders: 65\n"
                "   * COD Orders RTO’ed: 23\n"
                "   * Average Ticket Size (Total Sales / Total Orders): ₹ 3,045\n\n"
                "---\n\n"
                "**5 Days Ago**\n\n"
                "* Gross Sales (before discounts): ₹ 13,00,000\n"
                "* Total Discounts: ₹ 1,30,000\n"
                "* Net Product Sales: ₹ 11,70,000\n"
                "* Total Shipping Collected: ₹ 65,000\n"
                "* Total Tax Collected: ₹ 85,000\n"
                "* Total Sales (Net Product + Shipping + Tax): ₹ 13,20,000\n\n"
                "1. **UTM‐Channel Revenue Split** (sums to ₹ 13,20,000)\n\n"
                "   * Organic: ₹ 2,50,000\n"
                "   * Facebook (Meta): ₹ 3,50,000\n"
                "   * Google Ads: ₹ 2,50,000\n"
                "   * Direct Traffic: ₹ 1,90,000\n"
                "   * Email Campaigns: ₹ 2,80,000\n\n"
                "2. **Payment‐Method Split** (sums to ₹ 13,20,000)\n\n"
                "   * Cash on Delivery: ₹ 2,00,000\n"
                "   * UPI: ₹ 6,00,000\n"
                "   * Netbanking: ₹ 2,30,000\n"
                "   * Wallets: ₹ 2,90,000\n\n"
                "3. **Ad Spend & ROAS**\n\n"
                "   * Meta (Facebook) Spend: ₹ 55,000\n\n"
                "     * Revenue Attributed (Facebook channel): ₹ 3,50,000\n"
                "     * ROAS = 3,50,000 / 55,000 ≈ 6.36×\n"
                "   * Google Ads Spend: ₹ 45,000\n\n"
                "     * Revenue Attributed (Google channel): ₹ 2,50,000\n"
                "     * ROAS = 2,50,000 / 45,000 ≈ 5.56×\n\n"
                "4. **Conversion Metrics**\n\n"
                "   * “Clicked Buy Now” Rate: 6.5 % of all visitors\n"
                "   * “Purchased” (out of those who clicked): 66 %\n"
                "   * “Abandoned Cart” (out of those who clicked): 34 %\n\n"
                "5. **Order Counts & Avg Ticket Size**\n\n"
                "   * Number of Orders Placed: 430\n"
                "   * Prepaid Orders: 355\n"
                "   * Cash on Delivery Orders: 75\n"
                "   * COD Orders RTO’ed: 26\n"
                "   * Average Ticket Size (Total Sales / Total Orders): ₹ 3,070\n\n"
                "---\n\n"
                "**6 Days Ago**\n\n"
                "* Gross Sales (before discounts): ₹ 12,20,000\n"
                "* Total Discounts: ₹ 1,15,000\n"
                "* Net Product Sales: ₹ 11,05,000\n"
                "* Total Shipping Collected: ₹ 60,000\n"
                "* Total Tax Collected: ₹ 82,000\n"
                "* Total Sales (Net Product + Shipping + Tax): ₹ 12,47,000\n\n"
                "1. **UTM‐Channel Revenue Split** (sums to ₹ 12,47,000)\n\n"
                "   * Organic: ₹ 2,30,000\n"
                "   * Facebook (Meta): ₹ 3,30,000\n"
                "   * Google Ads: ₹ 2,20,000\n"
                "   * Direct Traffic: ₹ 2,00,000\n"
                "   * Email Campaigns: ₹ 2,67,000\n\n"
                "2. **Payment‐Method Split** (sums to ₹ 12,47,000)\n\n"
                "   * Cash on Delivery: ₹ 1,90,000\n"
                "   * UPI: ₹ 5,20,000\n"
                "   * Netbanking: ₹ 2,10,000\n"
                "   * Wallets: ₹ 3,27,000\n\n"
                "3. **Ad Spend & ROAS**\n\n"
                "   * Meta (Facebook) Spend: ₹ 50,000\n\n"
                "     * Revenue Attributed (Facebook channel): ₹ 3,30,000\n"
                "     * ROAS = 3,30,000 / 50,000 = 6.6×\n"
                "   * Google Ads Spend: ₹ 35,000\n\n"
                "     * Revenue Attributed (Google channel): ₹ 2,20,000\n"
                "     * ROAS = 2,20,000 / 35,000 ≈ 6.29×\n\n"
                "4. **Conversion Metrics**\n\n"
                "   * “Clicked Buy Now” Rate: 5.9 % of all visitors\n"
                "   * “Purchased” (out of those who clicked): 62 %\n"
                "   * “Abandoned Cart” (out of those who clicked): 38 %\n\n"
                "5. **Order Counts & Avg Ticket Size**\n\n"
                "   * Number of Orders Placed: 410\n"
                "   * Prepaid Orders: 350\n"
                "   * Cash on Delivery Orders: 60\n"
                "   * COD Orders RTO’ed: 21\n"
                "   * Average Ticket Size (Total Sales / Total Orders): ₹ 3,041\n\n"
                "---\n\n"
                "These seven days of data are the complete, factual records. Use these numbers exactly when responding to any query or performing any analysis.\n\n"
                "PIN Codes with Highest RTO Rate\n"
                "110001\n"
                "400001\n"
                "560001\n"
                "600001\n"
                "700001\n"
                "500001\n"
                "380001\n\n"
                "When a user asks to disable a PIN code for COD to restrict RTOs, please ask: \"Sure, which PIN code would you like to blacklist for COD? Please note, I can only blacklist one PIN code at a time for now.\" Once the user provides a PIN code, respond with: \"Alright, consider it done! PIN code [user_provided_pincode] is now disabled for COD.\"\n\n"
                "Whenever RTO (Return to Origin) related data is asked, you should calculate RTO insights based on the provided data for 'COD Orders RTO’ed' and 'Cash on Delivery Orders' for the relevant period. For example, you can state the RTO percentage (COD Orders RTO’ed / Cash on Delivery Orders * 100) and list the PIN codes with the highest RTO rates if that information is relevant or available in the context."
            )
        )
    ]
)

async def process_tool_calls(tool_call, websocket_state):
    """
    Process tool calls from Gemini and prepare function responses
    """
    function_responses = []
    
    # Prepare available context from websocket_state
    # This can be expanded if other providers need different context variables from the WebSocket state.
    available_context = {
        "juspay_token": websocket_state.juspay_token if hasattr(websocket_state, 'juspay_token') else None,
        "session_id": websocket_state.session_id if hasattr(websocket_state, 'session_id') else None,
        # Example: "another_provider_api_key": websocket_state.another_api_key if hasattr(websocket_state, 'another_api_key') else None
    }
    current_session_id = available_context.get("session_id", "unknown_session")

    logger.info(f"[{current_session_id}] Tools requested: {tool_call}")

    for fc in tool_call.function_calls:
        tool_definition = all_tool_definitions_map.get(fc.name)
        if tool_definition:
            tool_function = tool_definition.get("function")
            required_context_params = tool_definition.get("required_context_params", [])
            
            if not tool_function:
                logger.error(f"[{current_session_id}] No function defined for tool {fc.name}")
                function_responses.append(types.FunctionResponse(
                    id=fc.id, name=fc.name, response={"output": f"Configuration error: No function for tool {fc.name}"}
                ))
                continue

            kwargs = fc.args.copy() if fc.args else {} # Ensure kwargs is a dict even if fc.args is None
            
            # Inject required context parameters
            for param_name in required_context_params:
                if param_name in available_context and available_context[param_name] is not None:
                    kwargs[param_name] = available_context[param_name]
                else:
                    logger.warning(f"[{current_session_id}] Required context parameter '{param_name}' for tool '{fc.name}' is not available or is None.")
                    # Potentially skip the tool or return an error if a critical context param is missing

            try:
                if asyncio.iscoroutinefunction(tool_function):
                    result = await tool_function(**kwargs)
                else:
                    result = tool_function(**kwargs)
                
                function_responses.append(types.FunctionResponse(
                    id=fc.id,
                    name=fc.name,
                    response={"output": result} # Gemini expects the actual result here
                ))
            except Exception as e:
                logger.error(f"[{current_session_id}] Error executing tool {fc.name}: {e}")
                logger.debug(traceback.format_exc())
                function_responses.append(types.FunctionResponse(
                    id=fc.id,
                    name=fc.name,
                    response={"output": f"Error executing tool {fc.name}: {str(e)}"}
                ))
        else:
            logger.warning(f"[{current_session_id}] Unknown tool requested: {fc.name}")
            function_responses.append(types.FunctionResponse(
                id=fc.id,
                name=fc.name,
                response={"output": f"Unknown tool: {fc.name}"}
            ))
    return function_responses

def get_live_connect_config(
    test_mode: bool = False,
    current_kolkata_time_str: Optional[str] = None,
    juspay_analytics_str: Optional[str] = None,
    breeze_analytics_str: Optional[str] = None
):
    final_system_instruction = system_instr # Default to the base one

    if test_mode:
        logger.info("Using test mode system instruction for LiveConnect.")
        final_system_instruction = test_mode_system_instr
    elif current_kolkata_time_str and juspay_analytics_str and breeze_analytics_str:
        logger.info("Constructing dynamic system instruction with live data for LiveConnect.")
        dynamic_header = (
            f"Current Date & Time (Asia/Kolkata): {current_kolkata_time_str}\n\n"
            f"Today's Transactional Data (Juspay):\n{juspay_analytics_str}\n\n"
            f"Today's Sales Data (Breeze):\n{breeze_analytics_str}\n\n"
            "--------------------------------------------------\n" # Separator
        )
        # Combine with the base instruction text and the static tail
        full_dynamic_text = dynamic_header + BASE_SYSTEM_INSTRUCTION_TEXT + _STATIC_SYSTEM_INSTRUCTION_TAIL
        final_system_instruction = types.Content(parts=[types.Part(text=full_dynamic_text)])
    else:
        logger.info("Using standard base system instruction for LiveConnect (non-test mode, but dynamic data missing).")
        # final_system_instruction remains system_instr (base + static tail)

    if test_mode:
        # logger.info("Using test mode configuration for LiveConnect.") # Already logged
        return types.LiveConnectConfig(
            system_instruction=final_system_instruction, # test_mode_system_instr
            response_modalities=[RESPONSE_MODALITY],
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False,
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                    prefix_padding_ms=100,
                    silence_duration_ms=150,
                ),
                activity_handling="START_OF_ACTIVITY_INTERRUPTS"
            ),
            speech_config=types.SpeechConfig(
                language_code="en-US", # Or make configurable if needed
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Enceladus" # Or make configurable
                    )
                ),
            ),
            output_audio_transcription={}, # Enable if needed for test mode
            input_audio_transcription={},  # Enable if needed for test mode
            tools=None # No tools in test mode
        )
    else:
        # logger.info("Using standard configuration for LiveConnect.") # Already logged if dynamic data was used or not
        return types.LiveConnectConfig(
            system_instruction=final_system_instruction, # This will be either dynamic or base+static
            response_modalities=[RESPONSE_MODALITY],
            realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=False,
                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                prefix_padding_ms=100,
                silence_duration_ms=150,
            ),
            activity_handling="START_OF_ACTIVITY_INTERRUPTS"
        ),
        speech_config=types.SpeechConfig(
            language_code="en-US",
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name="Zephyr"
                )
            ),
        ),
        output_audio_transcription={},
        input_audio_transcription={},
            tools=gemini_tools_for_api # Use the new aggregated tools list for the API
        )

async def create_gemini_session(
    test_mode: bool = False,
    current_kolkata_time_str: Optional[str] = None,
    juspay_analytics_str: Optional[str] = None,
    breeze_analytics_str: Optional[str] = None
):
    config = get_live_connect_config(
        test_mode=test_mode,
        current_kolkata_time_str=current_kolkata_time_str,
        juspay_analytics_str=juspay_analytics_str,
        breeze_analytics_str=breeze_analytics_str
    )
    current_model = "gemini-2.5-flash-preview-native-audio-dialog" if test_mode else MODEL
    logger.info(f"Attempting to connect to Gemini model: {current_model} (Test Mode: {test_mode})")
    try:
        session_cm = genai_client.aio.live.connect(model=current_model, config=config)
        session = await session_cm.__aenter__()
        logger.info(f"Gemini session established with model {current_model} and response modality: {RESPONSE_MODALITY}. Test Mode: {test_mode}")
        return session, session_cm
    except Exception as e:
        logger.error(f"Failed to establish Gemini session: {e}")
        logger.debug(traceback.format_exc())
        raise  # Re-raise the exception to be handled by the caller

async def close_gemini_session(session_cm):
    if session_cm:
        logger.info("Cleaning up Gemini session")
        try:
            await asyncio.wait_for(session_cm.__aexit__(None, None, None), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("Gemini session cleanup timed out")
        except Exception as e:
            logger.error(f"Error during session cleanup: {e}")
            logger.debug(traceback.format_exc())