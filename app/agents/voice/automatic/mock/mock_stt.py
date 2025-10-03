from typing import List

from pipecat.frames.frames import TranscriptionFrame
from pipecat.processors.frame_processor import FrameProcessor

from app.core.logger import logger


class TestQuestionProcessor(FrameProcessor):
    """Processor that intercepts STT output and replaces trigger words with test questions"""

    def __init__(self, questions: List[str], name: str = "TestQuestionProcessor"):
        super().__init__(name=name)
        self.questions = questions
        self.current_question_index = -1  # Start at -1 so first "next" goes to index 0
        logger.info(
            "🎤 Test Question Processor: Ready. Say 'next' to start with first test question"
        )

    async def _create_test_question_frame(self, question_index):
        """Create a transcription frame with the test question"""
        if 0 <= question_index < len(self.questions):
            question = self.questions[question_index]
            logger.info(
                f"🎤 Test Question: Replacing with question {question_index+1}/{len(self.questions)}: '{question}'"
            )

            self.current_question_index = question_index
            return TranscriptionFrame(text=question, user_id="test_user", timestamp="")
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

            text_clean = text.translate(str.maketrans("", "", string.punctuation))
            words = text_clean.split()

            if "next" in words:
                # Move to next question in round-robin fashion
                next_index = (self.current_question_index + 1) % len(self.questions)
                logger.info(
                    f"🎤 Test Question: 'next' detected, moving to question {next_index + 1}"
                )
                test_frame = await self._create_test_question_frame(next_index)
                if test_frame:
                    await self.push_frame(test_frame, direction)
                return  # Don't pass the original "next" frame

            elif "repeat" in words:
                # Repeat current question
                if (
                    self.current_question_index >= 0
                ):  # Only if we have a current question
                    logger.info(
                        f"🎤 Test Question: 'repeat' detected, repeating question {self.current_question_index + 1}"
                    )
                    test_frame = await self._create_test_question_frame(
                        self.current_question_index
                    )
                    if test_frame:
                        await self.push_frame(test_frame, direction)
                else:
                    logger.info(
                        "🎤 Test Question: 'repeat' detected but no current question. Say 'next' first."
                    )
                return  # Don't pass the original "repeat" frame

            elif "back" in words:
                # Go to previous question
                if (
                    self.current_question_index >= 0
                ):  # Only if we have a current question
                    prev_index = (self.current_question_index - 1) % len(self.questions)
                    logger.info(
                        f"🎤 Test Question: 'back' detected, moving to question {prev_index + 1}"
                    )
                    test_frame = await self._create_test_question_frame(prev_index)
                    if test_frame:
                        await self.push_frame(test_frame, direction)
                else:
                    logger.info(
                        "🎤 Test Question: 'back' detected but no current question. Say 'next' first."
                    )
                return  # Don't pass the original "back" frame

        # Pass through all other frames normally
        await self.push_frame(frame, direction)


# Predefined question sets
DEFAULT_TEST_QUESTIONS = [
    "What is my conversion funnel for last week? Use random data",
    "How much revenue did I process through Cards/UPI/Netbanking/COD/Others yesterday? Use random data",
    "What is my conversion rate this month? Use random data",
    "What is the source of my leads last week? Use random data",
    "Can you provide marketing channel performance for this month? Use random data",
    "What is my ROAS for last week? Use random data",
    "What is my SR today? Use random data",
    "How many failed transactions did I have yesterday?",
    "What was the reason for the failed transactions yesterday?",
    "What is the daily trend for transaction success rates over the past week?",
    "What is the SR for different payment methods this week?",
    "What is the breakdown of payment methods last month?",
    "How many orders were placed today?",
    "What are my net sales this week?",
    "What are my prepaid sales last week?",
    "What's the forecast for sales based on the last 3 months data?",
    "What is AOV last week?",
    "What is my GMV this month?",
    "What are my COD sales last week?",
    "How many orders did I receive this week compared to last week?",
    "What regions have the highest sales last month?",
    "Which regions are we getting the most orders from this month?",
    "How many customers made a repeat purchase last month?",
    "Identify my top 10 most loyal customers based on purchase frequency in the last 6 months?",
    "How many new customers did I acquire this week?",
    "Which payment gateway is performing best in terms of success rates this month?",
    "How many abandoned carts did I have yesterday and what's their estimated value?",
    "iOS/Android - Device specific data for last week",
    "What is my Avg. number of orders per customer last month",
    "What are my top 10 most bought products this month?",
    "Which products are running low on stock currently?",
    "What are my current shipping rules?",
    "Can you block COD for certain pincodes?",
    "Can you block COD for certain customer numbers/emails?",
    "What are the high risk pincodes based on last month's data?",
    "Create a shipping rule basis cart value",
    "Create a shipping rule basis product",
    "Create a shipping basis pincode/regions",
    "Can you configure partial payment offer?",
    "Can you create a static offer for UPI?",
    "What was my sales growth this year compared to last year?",
    "What are my order details for order ID 12345",
    "What are my details for Transaction ID TXN67890",
    "Can you provide order analytics for the currently configured offers last week?",
    "Can you initiate a refund for order ID ORD54321",
    "What are my refund analytics yesterday?",
    "Can you bifurcate sales/SR basis payment method for last week. Provide more analytics like debit card, VPA, QR code",
    "Can you help with configuring Custom Payment Options for me?",
    "Can you please change the breeze checkout button skinning/colour?",
    "How many people applied offer SAVE20 during checkout last week? (Give as a percentage of total orders)",
    "How many carts used offers this week?",
    "How does offer affect their AOV and likelihood to purchase again last month?",
    "What is our Average Order Value (AOV) for first-time customers versus returning customers last quarter?",
    "Can you disable PayU as a PG",
    "What were the peak sales hours yesterday?",
    "Can you configure Surcharge on COD?",
    "What is the average sell through rate of the products this month?",
    "What products have the highest sell through rate last month?",
    "Which landing page is most visited by users first this week?",
    "How many orders were serviced through Standard, Express or any other shipping method last week?",
    "How many products do I currently have in my store? Active/inactive?",
    "How many of my orders are fulfilled? How many unfulfilled? How many partially fulfilled today?",
    "What has been the reach and impact of my campaigns last week?",
    "Can you comment on the effectiveness of campaign SUMMER2024 from last month?",
    "Which is the best performing adset this week?",
    "How much amount have I spent on the ads last month?",
    "How many campaigns did I run in the last 6 months?",
    "Which campaign has the highest spend but lowest ROAS last quarter?",
    "Show me the checkout behavior of customers who came from our Instagram ads versus those from Google Search last week",
    "Are new users converting better or returning users this month?",
    "What time of day are ads converting best this week?",
    "Suggest the top 3 performing campaigns I should scale based on last month's data.",
    "Compare Google vs Meta performance last week",
    "What is my CAC through ad campaign last month?",
    "Get me the details of my campaigns in breeze, which are live currently and what are they?",
    "Enable/Disable the payment method for a specific payment gateway.",
]

# You can create additional question sets if needed
# QUICK_TEST = ["What is my conversion rate?", "How many orders were placed?", "What is AOV?"]
