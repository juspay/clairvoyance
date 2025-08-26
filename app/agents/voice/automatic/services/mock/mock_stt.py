from typing import List
from app.core.logger import logger
from pipecat.frames.frames import TranscriptionFrame
from pipecat.processors.frame_processor import FrameProcessor


class TestQuestionProcessor(FrameProcessor):
    """Processor that intercepts STT output and replaces trigger words with test questions"""

    def __init__(self, questions: List[str], name: str = "TestQuestionProcessor"):
        super().__init__(name=name)
        self.questions = questions
        self.current_question_index = -1  # Start at -1 so first "next" goes to index 0
        logger.info("🎤 Test Question Processor: Ready. Say 'next' to start with first test question")

    async def _create_test_question_frame(self, question_index):
        """Create a transcription frame with the test question"""
        if 0 <= question_index < len(self.questions):
            question = self.questions[question_index]
            logger.info(f"🎤 Test Question: Replacing with question {question_index+1}/{len(self.questions)}: '{question}'")
            
            self.current_question_index = question_index
            return TranscriptionFrame(
                text=question,
                user_id="test_user", 
                timestamp=""
            )
        else:
            logger.info("🎤 Test Question: No more questions available")
            return None

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        
        # Intercept transcription frames and replace trigger words with test questions
        if isinstance(frame, TranscriptionFrame):
            text = frame.text.lower().strip()
            # Remove punctuation for better matching
            import string
            text_clean = text.translate(str.maketrans('', '', string.punctuation))
            words = text_clean.split()
            
            if "next" in words:
                # Move to next question in round-robin fashion
                next_index = (self.current_question_index + 1) % len(self.questions)
                logger.info(f"🎤 Test Question: 'next' detected, moving to question {next_index + 1}")
                test_frame = await self._create_test_question_frame(next_index)
                if test_frame:
                    await self.push_frame(test_frame, direction)
                return  # Don't pass the original "next" frame
                
            elif "repeat" in words:
                # Repeat current question
                if self.current_question_index >= 0:  # Only if we have a current question
                    logger.info(f"🎤 Test Question: 'repeat' detected, repeating question {self.current_question_index + 1}")
                    test_frame = await self._create_test_question_frame(self.current_question_index)
                    if test_frame:
                        await self.push_frame(test_frame, direction)
                else:
                    logger.info("🎤 Test Question: 'repeat' detected but no current question. Say 'next' first.")
                return  # Don't pass the original "repeat" frame
                
            elif "back" in words:
                # Go to previous question
                if self.current_question_index >= 0:  # Only if we have a current question
                    prev_index = (self.current_question_index - 1) % len(self.questions)
                    logger.info(f"🎤 Test Question: 'back' detected, moving to question {prev_index + 1}")
                    test_frame = await self._create_test_question_frame(prev_index)
                    if test_frame:
                        await self.push_frame(test_frame, direction)
                else:
                    logger.info("🎤 Test Question: 'back' detected but no current question. Say 'next' first.")
                return  # Don't pass the original "back" frame
        
        # Pass through all other frames normally
        await self.push_frame(frame, direction)


# Categorized question sets with metadata - following upstream DEFAULT_TEST_QUESTIONS order exactly
CATEGORIZED_TEST_QUESTIONS = [
    {"question": "What is my conversion funnel for last week?", "queryType": "read", "category": "breeze"},
    {"question": "How much revenue did I process through Cards/UPI/Netbanking/COD/Others yesterday?", "queryType": "read", "category": "juspay"},
    {"question": "What is my conversion rate this month?", "queryType": "read", "category": "breeze"},
    {"question": "What is the source of my leads last week?", "queryType": "read", "category": "marketing"},
    {"question": "Can you provide marketing channel performance for this month?", "queryType": "read", "category": "marketing"},
    {"question": "What is my ROAS for last week?", "queryType": "read", "category": "marketing"},
    {"question": "What is my SR today?", "queryType": "read", "category": "juspay"},
    {"question": "How many failed transactions did I have yesterday?", "queryType": "read", "category": "juspay"},
    {"question": "What was the reason for the failed transactions yesterday?", "queryType": "read", "category": "juspay"},
    {"question": "What is the daily trend for transaction success rates over the past week?", "queryType": "read", "category": "juspay"},
    {"question": "What is the SR for different payment methods this week?", "queryType": "read", "category": "juspay"},
    {"question": "What is the breakdown of payment methods last month?", "queryType": "read", "category": "juspay"},
    {"question": "How many orders were placed today?", "queryType": "read", "category": "breeze"},
    {"question": "What are my net sales this week?", "queryType": "read", "category": "breeze"},
    {"question": "What are my prepaid sales last week?", "queryType": "read", "category": "breeze"},
    {"question": "What's the forecast for sales based on the last 3 months data?", "queryType": "read", "category": "analytics"},
    {"question": "What is AOV last week?", "queryType": "read", "category": "breeze"},
    {"question": "What is my GMV this month?", "queryType": "read", "category": "breeze"},
    {"question": "What are my COD sales last week?", "queryType": "read", "category": "breeze"},
    {"question": "How many orders did I receive this week compared to last week?", "queryType": "read", "category": "breeze"},
    {"question": "What regions have the highest sales last month?", "queryType": "read", "category": "breeze"},
    {"question": "Which regions are we getting the most orders from this month?", "queryType": "read", "category": "breeze"},
    {"question": "How many customers made a repeat purchase last month?", "queryType": "read", "category": "breeze"},
    {"question": "Identify my top 10 most loyal customers based on purchase frequency in the last 6 months?", "queryType": "read", "category": "breeze"},
    {"question": "How many new customers did I acquire this week?", "queryType": "read", "category": "breeze"},
    {"question": "Which payment gateway is performing best in terms of success rates this month?", "queryType": "read", "category": "juspay"},
    {"question": "How many abandoned carts did I have yesterday and what's their estimated value?", "queryType": "read", "category": "breeze"},
    {"question": "iOS/Android - Device specific data for last week.", "queryType": "read", "category": "breeze"},
    {"question": "What is my Avg. number of orders per customer last month", "queryType": "read", "category": "breeze"},
    {"question": "What are my top 10 most bought products this month?", "queryType": "read", "category": "breeze"},
    {"question": "Which products are running low on stock currently?", "queryType": "read", "category": "breeze"},
    {"question": "What are my current shipping rules?", "queryType": "read", "category": "breeze"},
    {"question": "Can you block COD for certain pincodes?", "queryType": "write", "category": "breeze"},
    {"question": "Can you block COD for certain customer numbers/emails?", "queryType": "write", "category": "breeze"},
    {"question": "What are the high risk pincodes based on last month's data?", "queryType": "read", "category": "breeze"},
    {"question": "Create a shipping rule basis cart value", "queryType": "write", "category": "breeze"},
    {"question": "Create a shipping rule basis product", "queryType": "write", "category": "breeze"},
    {"question": "Create a shipping basis pincode/regions", "queryType": "write", "category": "breeze"},
    {"question": "Can you configure partial payment offer?", "queryType": "write", "category": "breeze"},
    {"question": "Can you create a static offer for UPI?", "queryType": "write", "category": "breeze"},
    {"question": "What was my sales growth this year compared to last year?", "queryType": "read", "category": "breeze"},
    {"question": "What are my order details for order ID 12345", "queryType": "read", "category": "breeze"},
    {"question": "What are my details for Transaction ID TXN67890", "queryType": "read", "category": "juspay"},
    {"question": "Can you provide order analytics for the currently configured offers last week?", "queryType": "read", "category": "breeze"},
    {"question": "Can you initiate a refund for order ID ORD54321", "queryType": "write", "category": "breeze"},
    {"question": "What are my refund analytics yesterday?", "queryType": "read", "category": "breeze"},
    {"question": "Can you bifurcate sales/SR basis payment method for last week. Provide more analytics like debit card, VPA, QR code.", "queryType": "read", "category": "juspay"},
    {"question": "Can you help with configuring Custom Payment Options for me?", "queryType": "write", "category": "juspay"},
    {"question": "Can you please change the breeze checkout button skinning/colour?", "queryType": "write", "category": "breeze"},
    {"question": "How many people applied offer SAVE20 during checkout last week? (Give as a percentage of total orders)", "queryType": "read", "category": "breeze"},
    {"question": "How many carts used offers this week?", "queryType": "read", "category": "breeze"},
    {"question": "How does offer affect their AOV and likelihood to purchase again last month?", "queryType": "read", "category": "breeze"},
    {"question": "What is our Average Order Value (AOV) for first-time customers versus returning customers last quarter?", "queryType": "read", "category": "breeze"},
    {"question": "Can you disable PayU as a PG", "queryType": "write", "category": "juspay"},
    {"question": "What were the peak sales hours yesterday?", "queryType": "read", "category": "breeze"},
    {"question": "Can you configure Surcharge on COD?", "queryType": "write", "category": "juspay"},
    {"question": "What is the average sell through rate of the products this month?", "queryType": "read", "category": "breeze"},
    {"question": "What products have the highest sell through rate last month?", "queryType": "read", "category": "breeze"},
    {"question": "Which landing page is most visited by users first this week?", "queryType": "read", "category": "breeze"},
    {"question": "How many orders were serviced through Standard, Express or any other shipping method last week?", "queryType": "read", "category": "breeze"},
    {"question": "How many products do I currently have in my store? Active/inactive?", "queryType": "read", "category": "breeze"},
    {"question": "How many of my orders are fulfilled? How many unfulfilled? How many partially fulfilled today?", "queryType": "read", "category": "breeze"},
    {"question": "What has been the reach and impact of my campaigns last week?", "queryType": "read", "category": "marketing"},
    {"question": "Can you comment on the effectiveness of campaign SUMMER2024 from last month?", "queryType": "read", "category": "marketing"},
    {"question": "Which is the best performing adset this week?", "queryType": "read", "category": "marketing"},
    {"question": "How much amount have I spent on the ads last month?", "queryType": "read", "category": "marketing"},
    {"question": "How many campaigns did I run in the last 6 months?", "queryType": "read", "category": "marketing"},
    {"question": "Which campaign has the highest spend but lowest ROAS last quarter?", "queryType": "read", "category": "marketing"},
    {"question": "Show me the checkout behavior of customers who came from our Instagram ads versus those from Google Search last week.", "queryType": "read", "category": "marketing"},
    {"question": "Are new users converting better or returning users this month?", "queryType": "read", "category": "marketing"},
    {"question": "What time of day are ads converting best this week?", "queryType": "read", "category": "marketing"},
    {"question": "Suggest the top 3 performing campaigns I should scale based on last month's data.", "queryType": "write", "category": "marketing"},
    {"question": "Compare Google vs Meta performance last week.", "queryType": "read", "category": "marketing"},
    {"question": "What is my CAC through ad campaign last month?", "queryType": "read", "category": "marketing"},
    {"question": "Get me the details of my campaigns in breeze, which are live currently and what are they?", "queryType": "read", "category": "breeze"},
    {"question": "Enable/Disable the payment method for a specific payment gateway.", "queryType": "write", "category": "juspay"},
    {"question": "What is my number of current live visitors on my Shopify store?", "queryType": "read", "category": "shopify"},
    {"question": "What is the split of sales across the channels - Online store, Buy Button, Social media etc on Shopify?", "queryType": "read", "category": "shopify"},
    {"question": "What is the number of user sessions in my Shopify online store?", "queryType": "read", "category": "shopify"},
    {"question": "What is my gross sale on Shopify?", "queryType": "read", "category": "shopify"},
    {"question": "Compare my Shopify sales to Yesterday, Last week, last month, previous quarter, previous year.", "queryType": "read", "category": "shopify"},
    {"question": "Give me Shopify sales breakdown - Gross sales, Discounts, Returns, Net sales, Shipping charges, Return fees, Taxes, Total sales.", "queryType": "read", "category": "shopify"},
    {"question": "What is my Total sales by product on Shopify? (Total sales, broken down by product. Total sales = net sales + additional fees + duties + shipping charges + taxes)", "queryType": "read", "category": "shopify"},
    {"question": "What is my Shopify Sessions by location? (Sessions in your online store, broken down by geographic location)", "queryType": "read", "category": "shopify"},
    {"question": "What is Total sales by social referrer split on Shopify? (Total sales from social sources, broken down by name)", "queryType": "read", "category": "shopify"},
    {"question": "What is my Shopify Sessions by landing page breakdown? (Sessions in your online store, broken down by the page the user first landed on)", "queryType": "read", "category": "shopify"},
    {"question": "What split of Sales attributed to marketing on Shopify? (Sales from trackable marketing efforts)", "queryType": "read", "category": "shopify"},
    {"question": "List of Products that are out of stock on my Shopify store?", "queryType": "read", "category": "shopify"},
    {"question": "Give me Shopify inventory details that are available & variants data?", "queryType": "read", "category": "shopify"},
    {"question": "What is the payment status of the Shopify Order XYZ?", "queryType": "read", "category": "shopify"},
    {"question": "What are the Shopify orders in Payment status - Pending/Paid/Expired/Voided?", "queryType": "read", "category": "shopify"},
]

# Generate the DEFAULT_TEST_QUESTIONS from CATEGORIZED_TEST_QUESTIONS to ensure consistency
DEFAULT_TEST_QUESTIONS = [item["question"] for item in CATEGORIZED_TEST_QUESTIONS]

# Utility functions to filter questions
def get_questions_by_category(category):
    """Get all questions for a specific category"""
    return [item for item in CATEGORIZED_TEST_QUESTIONS if item["category"] == category]

def get_questions_by_type(query_type):
    """Get all questions by query type (read/write)"""
    return [item for item in CATEGORIZED_TEST_QUESTIONS if item["queryType"] == query_type]

def get_questions_by_category_and_type(category, query_type):
    """Get questions filtered by both category and type"""
    return [item for item in CATEGORIZED_TEST_QUESTIONS if item["category"] == category and item["queryType"] == query_type]

def get_question_metadata(question):
    """Get metadata for a specific question"""
    for item in CATEGORIZED_TEST_QUESTIONS:
        if item["question"] == question:
            return {"queryType": item["queryType"], "category": item["category"]}
    return None

# You can create additional question sets if needed
# QUICK_TEST = ["What is my conversion rate?", "How many orders were placed?", "What is AOV?"]