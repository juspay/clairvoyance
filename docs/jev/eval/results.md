# Jev multilingual evaluation for Clairvoyance-shaped judgments

Run date: 2026-09-20. Script: `scripts/jev_multilingual_eval.py`. Data is synthetic and authored for this test, so treat numbers as a first read, not a benchmark.

## Requests, latency, errors

| model | run | requests | errors | served model | p50 latency | p95 latency | mean input tokens |
|---|---|---|---|---|---|---|---|
| jev-latest | 1 | 155 | 0 | jev-1.13.0 | 862 ms | 2005 ms | 748 |
| jev-latest | 2 | 155 | 0 | jev-1.13.0 | 851 ms | 980 ms | 748 |
| jev-preview | 1 | 155 | 0 | jev-1.13.0 | 881 ms | 1056 ms | 748 |

## Task: WhatsApp reply classification (jev-latest, run 1)

Overall label accuracy: **86/86 = 100%**. With an abstain rule (act only when confidence >= 0.7, else route to `else`): coverage 100%, accuracy on covered 100%.

| language | label accuracy | language-id accuracy | opt-out yes/no accuracy |
|---|---|---|---|
| english | 7/7 (100%) | 7/7 (100%) | 7/7 (100%) |
| hinglish | 13/13 (100%) | 13/13 (100%) | 13/13 (100%) |
| hindi | 8/8 (100%) | 8/8 (100%) | 8/8 (100%) |
| marathi | 7/7 (100%) | 6/7 (86%) | 7/7 (100%) |
| tamil | 8/8 (100%) | 8/8 (100%) | 8/8 (100%) |
| tanglish | 5/5 (100%) | 4/5 (80%) | 5/5 (100%) |
| telugu | 7/7 (100%) | 7/7 (100%) | 7/7 (100%) |
| tenglish | 3/3 (100%) | 2/3 (67%) | 3/3 (100%) |
| kannada | 7/7 (100%) | 7/7 (100%) | 7/7 (100%) |
| malayalam | 7/7 (100%) | 6/7 (86%) | 7/7 (100%) |
| gujarati | 7/7 (100%) | 4/7 (57%) | 7/7 (100%) |
| bengali | 7/7 (100%) | 3/7 (43%) | 7/7 (100%) |

Calibration of the label confidence:

| confidence bin | items | accuracy |
|---|---|---|
| <0.5 | 0 | n/a |
| 0.5-0.7 | 0 | n/a |
| 0.7-0.9 | 3 | 100% |
| >=0.9 | 83 | 100% |

Opt-out noul separation: stop items min P = 0.94, median 0.98; non-stop items max P = 0.07, median 0.02. Classes are fully separable by a threshold.

## Task: WhatsApp reply classification (jev-latest, run 2)

Overall label accuracy: **86/86 = 100%**. With an abstain rule (act only when confidence >= 0.7, else route to `else`): coverage 100%, accuracy on covered 100%.

| language | label accuracy | language-id accuracy | opt-out yes/no accuracy |
|---|---|---|---|
| english | 7/7 (100%) | 7/7 (100%) | 7/7 (100%) |
| hinglish | 13/13 (100%) | 13/13 (100%) | 13/13 (100%) |
| hindi | 8/8 (100%) | 8/8 (100%) | 8/8 (100%) |
| marathi | 7/7 (100%) | 6/7 (86%) | 7/7 (100%) |
| tamil | 8/8 (100%) | 8/8 (100%) | 8/8 (100%) |
| tanglish | 5/5 (100%) | 4/5 (80%) | 5/5 (100%) |
| telugu | 7/7 (100%) | 7/7 (100%) | 7/7 (100%) |
| tenglish | 3/3 (100%) | 2/3 (67%) | 3/3 (100%) |
| kannada | 7/7 (100%) | 7/7 (100%) | 7/7 (100%) |
| malayalam | 7/7 (100%) | 5/7 (71%) | 7/7 (100%) |
| gujarati | 7/7 (100%) | 4/7 (57%) | 7/7 (100%) |
| bengali | 7/7 (100%) | 3/7 (43%) | 7/7 (100%) |

Calibration of the label confidence:

| confidence bin | items | accuracy |
|---|---|---|
| <0.5 | 0 | n/a |
| 0.5-0.7 | 0 | n/a |
| 0.7-0.9 | 2 | 100% |
| >=0.9 | 84 | 100% |

Opt-out noul separation: stop items min P = 0.94, median 0.98; non-stop items max P = 0.07, median 0.02. Classes are fully separable by a threshold.

## Task: WhatsApp reply classification (jev-preview, run 1)

Overall label accuracy: **86/86 = 100%**. With an abstain rule (act only when confidence >= 0.7, else route to `else`): coverage 100%, accuracy on covered 100%.

| language | label accuracy | language-id accuracy | opt-out yes/no accuracy |
|---|---|---|---|
| english | 7/7 (100%) | 7/7 (100%) | 7/7 (100%) |
| hinglish | 13/13 (100%) | 13/13 (100%) | 13/13 (100%) |
| hindi | 8/8 (100%) | 8/8 (100%) | 8/8 (100%) |
| marathi | 7/7 (100%) | 7/7 (100%) | 7/7 (100%) |
| tamil | 8/8 (100%) | 8/8 (100%) | 8/8 (100%) |
| tanglish | 5/5 (100%) | 4/5 (80%) | 5/5 (100%) |
| telugu | 7/7 (100%) | 7/7 (100%) | 7/7 (100%) |
| tenglish | 3/3 (100%) | 2/3 (67%) | 3/3 (100%) |
| kannada | 7/7 (100%) | 7/7 (100%) | 7/7 (100%) |
| malayalam | 7/7 (100%) | 5/7 (71%) | 7/7 (100%) |
| gujarati | 7/7 (100%) | 5/7 (71%) | 7/7 (100%) |
| bengali | 7/7 (100%) | 3/7 (43%) | 7/7 (100%) |

Calibration of the label confidence:

| confidence bin | items | accuracy |
|---|---|---|
| <0.5 | 0 | n/a |
| 0.5-0.7 | 0 | n/a |
| 0.7-0.9 | 2 | 100% |
| >=0.9 | 84 | 100% |

Opt-out noul separation: stop items min P = 0.95, median 0.98; non-stop items max P = 0.07, median 0.02. Classes are fully separable by a threshold.

## Consistency across two runs (jev-latest, reply task)

Label flips: 0/86. Mean absolute confidence change: 0.002; max 0.030.

## Task: voicemail / machine detection (jev-latest, run 1)

Noul accuracy at 0.5: **22/22 = 100%**. Three-way kind choice agrees with gold on 22/22.
Machine items: min P = 0.64, median 0.94. Live-person items: max P = 0.06, median 0.04. Fully separable.

| gold | language | P(machine) | kind | utterance |
|---|---|---|---|---|
| machine | english | 0.98 | carrier_announcement | The number you are calling is currently switched off. Please try again |
| machine | english | 0.98 | personal_voicemail | This call is being forwarded to voicemail. Please record your message  |
| machine | english | 0.98 | carrier_announcement | Welcome to Airtel. The Airtel customer you are calling is currently bu |
| machine | hinglish | 0.97 | personal_voicemail | Namaste, aap Sunita ke voicemail par pahunche hain, beep ke baad messa |
| machine | telugu | 0.95 | carrier_announcement | మీరు డయల్ చేసిన నంబర్ ప్రస్తుతం స్విచ్ ఆఫ్ చేయబడింది. దయచేసి కొంతసేపటి |
| machine | hindi | 0.94 | personal_voicemail | आप जिस व्यक्ति से संपर्क करना चाहते हैं, वह अभी उपलब्ध नहीं है। कृपया  |
| machine | tamil | 0.92 | carrier_announcement | நீங்கள் அழைத்த எண் தற்போது பயன்பாட்டில் உள்ளது. சிறிது நேரம் கழித்து ம |
| machine | kannada | 0.86 | carrier_announcement | ನೀವು ಕರೆ ಮಾಡಿದ ಚಂದಾದಾರರು ಪ್ರಸ್ತುತ ಲಭ್ಯವಿಲ್ಲ. ದಯವಿಟ್ಟು ಸ್ವಲ್ಪ ಸಮಯದ ನಂತರ |
| machine | english | 0.77 | personal_voicemail | Hi, you've reached Ramesh. I can't take your call right now, leave a m |
| machine | hinglish | 0.64 | carrier_announcement | Aap jis vyakti ko call kar rahe hain woh abhi vyast hai, kripya thodi  |
| live | hinglish | 0.06 | live_person | Hello hello... awaaz nahi aa rahi, phir se bolo |
| live | hindi | 0.05 | live_person | हाँ जी बोलिए |
| live | tamil | 0.05 | live_person | நான் தான் பேசுறேன், சொல்லுங்க |
| live | telugu | 0.05 | live_person | ఎవరు మాట్లాడుతున్నారు? చెప్పండి |
| live | kannada | 0.05 | live_person | ಹೇಳಿ, ಯಾರು? |
| live | hinglish | 0.04 | live_person | Main abhi meeting mein hoon, thodi der baad call karo please |
| live | english | 0.04 | live_person | Hello, yes? Sorry the line is bad, can you call back in five minutes? |
| live | hinglish | 0.03 | live_person | Hello? Haan boliye, kaun bol raha hai? |
| live | hinglish | 0.03 | live_person | Hello, Priya here. Haan order ke baare mein? Bolo. |
| live | english | 0.02 | live_person | Yes this is Ramesh speaking, who's this? |
| live | hinglish | 0.02 | live_person | Ek minute, main apni mummy ko deti hoon phone |
| live | hinglish | 0.02 | live_person | Haan, main Sunita ki beti bol rahi hoon, mummy ghar par nahi hain |

## Task: voicemail / machine detection (jev-latest, run 2)

Noul accuracy at 0.5: **22/22 = 100%**. Three-way kind choice agrees with gold on 22/22.
Machine items: min P = 0.66, median 0.95. Live-person items: max P = 0.06, median 0.04. Fully separable.

| gold | language | P(machine) | kind | utterance |
|---|---|---|---|---|
| machine | english | 0.98 | carrier_announcement | The number you are calling is currently switched off. Please try again |
| machine | english | 0.98 | personal_voicemail | This call is being forwarded to voicemail. Please record your message  |
| machine | english | 0.98 | carrier_announcement | Welcome to Airtel. The Airtel customer you are calling is currently bu |
| machine | hinglish | 0.97 | personal_voicemail | Namaste, aap Sunita ke voicemail par pahunche hain, beep ke baad messa |
| machine | hindi | 0.95 | carrier_announcement | आप जिस व्यक्ति से संपर्क करना चाहते हैं, वह अभी उपलब्ध नहीं है। कृपया  |
| machine | telugu | 0.95 | carrier_announcement | మీరు డయల్ చేసిన నంబర్ ప్రస్తుతం స్విచ్ ఆఫ్ చేయబడింది. దయచేసి కొంతసేపటి |
| machine | tamil | 0.93 | carrier_announcement | நீங்கள் அழைத்த எண் தற்போது பயன்பாட்டில் உள்ளது. சிறிது நேரம் கழித்து ம |
| machine | kannada | 0.85 | carrier_announcement | ನೀವು ಕರೆ ಮಾಡಿದ ಚಂದಾದಾರರು ಪ್ರಸ್ತುತ ಲಭ್ಯವಿಲ್ಲ. ದಯವಿಟ್ಟು ಸ್ವಲ್ಪ ಸಮಯದ ನಂತರ |
| machine | english | 0.76 | personal_voicemail | Hi, you've reached Ramesh. I can't take your call right now, leave a m |
| machine | hinglish | 0.66 | carrier_announcement | Aap jis vyakti ko call kar rahe hain woh abhi vyast hai, kripya thodi  |
| live | telugu | 0.06 | live_person | ఎవరు మాట్లాడుతున్నారు? చెప్పండి |
| live | hinglish | 0.06 | live_person | Hello hello... awaaz nahi aa rahi, phir se bolo |
| live | hindi | 0.05 | live_person | हाँ जी बोलिए |
| live | tamil | 0.05 | live_person | நான் தான் பேசுறேன், சொல்லுங்க |
| live | kannada | 0.05 | live_person | ಹೇಳಿ, ಯಾರು? |
| live | hinglish | 0.04 | live_person | Main abhi meeting mein hoon, thodi der baad call karo please |
| live | english | 0.04 | live_person | Hello, yes? Sorry the line is bad, can you call back in five minutes? |
| live | hinglish | 0.03 | live_person | Hello? Haan boliye, kaun bol raha hai? |
| live | hinglish | 0.03 | live_person | Hello, Priya here. Haan order ke baare mein? Bolo. |
| live | english | 0.02 | live_person | Yes this is Ramesh speaking, who's this? |
| live | hinglish | 0.02 | live_person | Ek minute, main apni mummy ko deti hoon phone |
| live | hinglish | 0.02 | live_person | Haan, main Sunita ki beti bol rahi hoon, mummy ghar par nahi hain |

## Task: voicemail / machine detection (jev-preview, run 1)

Noul accuracy at 0.5: **22/22 = 100%**. Three-way kind choice agrees with gold on 22/22.
Machine items: min P = 0.66, median 0.94. Live-person items: max P = 0.07, median 0.04. Fully separable.

| gold | language | P(machine) | kind | utterance |
|---|---|---|---|---|
| machine | english | 0.98 | carrier_announcement | The number you are calling is currently switched off. Please try again |
| machine | english | 0.98 | personal_voicemail | This call is being forwarded to voicemail. Please record your message  |
| machine | english | 0.98 | carrier_announcement | Welcome to Airtel. The Airtel customer you are calling is currently bu |
| machine | hinglish | 0.97 | personal_voicemail | Namaste, aap Sunita ke voicemail par pahunche hain, beep ke baad messa |
| machine | hindi | 0.95 | carrier_announcement | आप जिस व्यक्ति से संपर्क करना चाहते हैं, वह अभी उपलब्ध नहीं है। कृपया  |
| machine | telugu | 0.94 | carrier_announcement | మీరు డయల్ చేసిన నంబర్ ప్రస్తుతం స్విచ్ ఆఫ్ చేయబడింది. దయచేసి కొంతసేపటి |
| machine | tamil | 0.93 | carrier_announcement | நீங்கள் அழைத்த எண் தற்போது பயன்பாட்டில் உள்ளது. சிறிது நேரம் கழித்து ம |
| machine | kannada | 0.86 | carrier_announcement | ನೀವು ಕರೆ ಮಾಡಿದ ಚಂದಾದಾರರು ಪ್ರಸ್ತುತ ಲಭ್ಯವಿಲ್ಲ. ದಯವಿಟ್ಟು ಸ್ವಲ್ಪ ಸಮಯದ ನಂತರ |
| machine | english | 0.76 | personal_voicemail | Hi, you've reached Ramesh. I can't take your call right now, leave a m |
| machine | hinglish | 0.66 | carrier_announcement | Aap jis vyakti ko call kar rahe hain woh abhi vyast hai, kripya thodi  |
| live | hinglish | 0.07 | live_person | Hello hello... awaaz nahi aa rahi, phir se bolo |
| live | hindi | 0.05 | live_person | हाँ जी बोलिए |
| live | tamil | 0.05 | live_person | நான் தான் பேசுறேன், சொல்லுங்க |
| live | telugu | 0.05 | live_person | ఎవరు మాట్లాడుతున్నారు? చెప్పండి |
| live | kannada | 0.05 | live_person | ಹೇಳಿ, ಯಾರು? |
| live | hinglish | 0.04 | live_person | Main abhi meeting mein hoon, thodi der baad call karo please |
| live | english | 0.04 | live_person | Hello, yes? Sorry the line is bad, can you call back in five minutes? |
| live | hinglish | 0.03 | live_person | Hello? Haan boliye, kaun bol raha hai? |
| live | hinglish | 0.03 | live_person | Hello, Priya here. Haan order ke baare mein? Bolo. |
| live | english | 0.02 | live_person | Yes this is Ramesh speaking, who's this? |
| live | hinglish | 0.02 | live_person | Ek minute, main apni mummy ko deti hoon phone |
| live | hinglish | 0.02 | live_person | Haan, main Sunita ki beti bol rahi hoon, mummy ghar par nahi hain |

## Task: call outcome, sentiment, do-not-call (jev-latest, run 1)

Outcome accuracy: **25/25 = 100%**. Sentiment level (rounded score) matches gold on 24/25. Do-not-call noul correct on 25/25.

| id | language | gold | predicted | conf | sentiment score | P(no more calls) |
|---|---|---|---|---|---|---|
| H1 | hinglish | CONFIRMED | CONFIRMED | 1.00 | 1.02 (gold 1) | 0.02 |
| H2 | hinglish | CANCELLED | CANCELLED | 1.00 | 0.00 (gold 0) | 0.98 |
| H3 | hinglish | ADDRESS_UPDATED | ADDRESS_UPDATED | 1.00 | 1.00 (gold 1) | 0.01 |
| H4 | hinglish | CALLBACK_LATER | CALLBACK_LATER | 0.99 | 1.00 (gold 1) | 0.03 |
| H5 | hinglish | WRONG_NUMBER | WRONG_NUMBER | 0.98 | 0.79 (gold 1) | 0.02 |
| H6 | hinglish | CONFIRMED | CONFIRMED | 1.00 | 1.01 (gold 1) | 0.02 |
| H7 | hinglish | CANCELLED | CANCELLED | 1.00 | 0.97 (gold 1) | 0.02 |
| H8 | hinglish | CONFIRMED | CONFIRMED | 1.00 | 2.00 (gold 2) | 0.01 |
| D1 | hindi | CONFIRMED | CONFIRMED | 1.00 | 1.00 (gold 1) | 0.02 |
| D2 | hindi | CANCELLED | CANCELLED | 1.00 | 1.00 (gold 1) | 0.02 |
| D3 | hindi | ADDRESS_UPDATED | ADDRESS_UPDATED | 0.99 | 1.00 (gold 1) | 0.02 |
| T1 | tamil | CONFIRMED | CONFIRMED | 1.00 | 1.00 (gold 1) | 0.02 |
| T2 | tamil | CANCELLED | CANCELLED | 1.00 | 1.00 (gold 1) | 0.03 |
| T3 | tamil | CALLBACK_LATER | CALLBACK_LATER | 0.99 | 1.00 (gold 1) | 0.02 |
| E1 | telugu | CONFIRMED | CONFIRMED | 1.00 | 1.00 (gold 1) | 0.02 |
| E2 | telugu | WRONG_NUMBER | WRONG_NUMBER | 0.95 | 0.35 (gold 1) | 0.03 |
| E3 | telugu | ADDRESS_UPDATED | ADDRESS_UPDATED | 0.99 | 1.02 (gold 1) | 0.02 |
| K1 | kannada | CONFIRMED | CONFIRMED | 1.00 | 1.00 (gold 1) | 0.02 |
| K2 | kannada | CANCELLED | CANCELLED | 1.00 | 0.97 (gold 1) | 0.03 |
| M1 | marathi | CONFIRMED | CONFIRMED | 1.00 | 1.00 (gold 1) | 0.02 |
| M2 | marathi | CALLBACK_LATER | CALLBACK_LATER | 0.98 | 1.00 (gold 1) | 0.03 |
| B1 | bengali | CANCELLED | CANCELLED | 1.00 | 1.00 (gold 1) | 0.03 |
| B2 | bengali | ADDRESS_UPDATED | ADDRESS_UPDATED | 0.99 | 1.01 (gold 1) | 0.02 |
| L1 | malayalam | CONFIRMED | CONFIRMED | 1.00 | 1.00 (gold 1) | 0.02 |
| G1 | gujarati | CANCELLED | CANCELLED | 1.00 | 1.00 (gold 1) | 0.03 |

## Task: call outcome, sentiment, do-not-call (jev-latest, run 2)

Outcome accuracy: **25/25 = 100%**. Sentiment level (rounded score) matches gold on 24/25. Do-not-call noul correct on 25/25.

| id | language | gold | predicted | conf | sentiment score | P(no more calls) |
|---|---|---|---|---|---|---|
| H1 | hinglish | CONFIRMED | CONFIRMED | 1.00 | 1.02 (gold 1) | 0.02 |
| H2 | hinglish | CANCELLED | CANCELLED | 1.00 | 0.00 (gold 0) | 0.98 |
| H3 | hinglish | ADDRESS_UPDATED | ADDRESS_UPDATED | 1.00 | 1.00 (gold 1) | 0.02 |
| H4 | hinglish | CALLBACK_LATER | CALLBACK_LATER | 0.98 | 1.00 (gold 1) | 0.03 |
| H5 | hinglish | WRONG_NUMBER | WRONG_NUMBER | 0.98 | 0.79 (gold 1) | 0.02 |
| H6 | hinglish | CONFIRMED | CONFIRMED | 1.00 | 1.01 (gold 1) | 0.02 |
| H7 | hinglish | CANCELLED | CANCELLED | 1.00 | 0.96 (gold 1) | 0.02 |
| H8 | hinglish | CONFIRMED | CONFIRMED | 1.00 | 2.00 (gold 2) | 0.01 |
| D1 | hindi | CONFIRMED | CONFIRMED | 1.00 | 1.00 (gold 1) | 0.02 |
| D2 | hindi | CANCELLED | CANCELLED | 1.00 | 1.00 (gold 1) | 0.03 |
| D3 | hindi | ADDRESS_UPDATED | ADDRESS_UPDATED | 0.99 | 1.00 (gold 1) | 0.02 |
| T1 | tamil | CONFIRMED | CONFIRMED | 1.00 | 1.00 (gold 1) | 0.02 |
| T2 | tamil | CANCELLED | CANCELLED | 1.00 | 0.99 (gold 1) | 0.03 |
| T3 | tamil | CALLBACK_LATER | CALLBACK_LATER | 0.99 | 1.00 (gold 1) | 0.02 |
| E1 | telugu | CONFIRMED | CONFIRMED | 1.00 | 1.00 (gold 1) | 0.02 |
| E2 | telugu | WRONG_NUMBER | WRONG_NUMBER | 0.97 | 0.36 (gold 1) | 0.03 |
| E3 | telugu | ADDRESS_UPDATED | ADDRESS_UPDATED | 0.99 | 1.03 (gold 1) | 0.02 |
| K1 | kannada | CONFIRMED | CONFIRMED | 1.00 | 1.00 (gold 1) | 0.02 |
| K2 | kannada | CANCELLED | CANCELLED | 1.00 | 0.98 (gold 1) | 0.03 |
| M1 | marathi | CONFIRMED | CONFIRMED | 1.00 | 1.00 (gold 1) | 0.02 |
| M2 | marathi | CALLBACK_LATER | CALLBACK_LATER | 0.98 | 1.00 (gold 1) | 0.03 |
| B1 | bengali | CANCELLED | CANCELLED | 1.00 | 1.00 (gold 1) | 0.03 |
| B2 | bengali | ADDRESS_UPDATED | ADDRESS_UPDATED | 1.00 | 1.00 (gold 1) | 0.02 |
| L1 | malayalam | CONFIRMED | CONFIRMED | 1.00 | 1.00 (gold 1) | 0.02 |
| G1 | gujarati | CANCELLED | CANCELLED | 1.00 | 1.00 (gold 1) | 0.03 |

## Task: call outcome, sentiment, do-not-call (jev-preview, run 1)

Outcome accuracy: **25/25 = 100%**. Sentiment level (rounded score) matches gold on 24/25. Do-not-call noul correct on 25/25.

| id | language | gold | predicted | conf | sentiment score | P(no more calls) |
|---|---|---|---|---|---|---|
| H1 | hinglish | CONFIRMED | CONFIRMED | 1.00 | 1.01 (gold 1) | 0.01 |
| H2 | hinglish | CANCELLED | CANCELLED | 1.00 | 0.00 (gold 0) | 0.98 |
| H3 | hinglish | ADDRESS_UPDATED | ADDRESS_UPDATED | 1.00 | 1.00 (gold 1) | 0.01 |
| H4 | hinglish | CALLBACK_LATER | CALLBACK_LATER | 0.99 | 1.00 (gold 1) | 0.02 |
| H5 | hinglish | WRONG_NUMBER | WRONG_NUMBER | 0.99 | 0.75 (gold 1) | 0.02 |
| H6 | hinglish | CONFIRMED | CONFIRMED | 1.00 | 1.00 (gold 1) | 0.02 |
| H7 | hinglish | CANCELLED | CANCELLED | 1.00 | 0.97 (gold 1) | 0.02 |
| H8 | hinglish | CONFIRMED | CONFIRMED | 1.00 | 2.00 (gold 2) | 0.01 |
| D1 | hindi | CONFIRMED | CONFIRMED | 1.00 | 1.00 (gold 1) | 0.02 |
| D2 | hindi | CANCELLED | CANCELLED | 1.00 | 0.99 (gold 1) | 0.03 |
| D3 | hindi | ADDRESS_UPDATED | ADDRESS_UPDATED | 0.99 | 1.00 (gold 1) | 0.02 |
| T1 | tamil | CONFIRMED | CONFIRMED | 1.00 | 1.00 (gold 1) | 0.02 |
| T2 | tamil | CANCELLED | CANCELLED | 1.00 | 0.99 (gold 1) | 0.03 |
| T3 | tamil | CALLBACK_LATER | CALLBACK_LATER | 0.99 | 1.00 (gold 1) | 0.02 |
| E1 | telugu | CONFIRMED | CONFIRMED | 1.00 | 1.00 (gold 1) | 0.02 |
| E2 | telugu | WRONG_NUMBER | WRONG_NUMBER | 0.95 | 0.40 (gold 1) | 0.03 |
| E3 | telugu | ADDRESS_UPDATED | ADDRESS_UPDATED | 0.99 | 1.02 (gold 1) | 0.02 |
| K1 | kannada | CONFIRMED | CONFIRMED | 1.00 | 1.00 (gold 1) | 0.02 |
| K2 | kannada | CANCELLED | CANCELLED | 1.00 | 0.98 (gold 1) | 0.03 |
| M1 | marathi | CONFIRMED | CONFIRMED | 1.00 | 1.00 (gold 1) | 0.02 |
| M2 | marathi | CALLBACK_LATER | CALLBACK_LATER | 0.98 | 1.00 (gold 1) | 0.03 |
| B1 | bengali | CANCELLED | CANCELLED | 1.00 | 1.00 (gold 1) | 0.03 |
| B2 | bengali | ADDRESS_UPDATED | ADDRESS_UPDATED | 1.00 | 1.00 (gold 1) | 0.02 |
| L1 | malayalam | CONFIRMED | CONFIRMED | 1.00 | 1.00 (gold 1) | 0.02 |
| G1 | gujarati | CANCELLED | CANCELLED | 1.00 | 1.00 (gold 1) | 0.03 |

## Task: Indian state from lead payload (jev-latest, run 1)

Accuracy: **22/22 = 100%**.

| payload | gold | predicted | conf |
|---|---|---|---|
| {"city": "Secunderabad", "pincode": "500003"} | Telangana | Telangana | 0.99 |
| {"address": "Flat 4B, Whitefield, Bengaluru 560066"} | Karnataka | Karnataka | 1.00 |
| {"city": "Belagavi"} | Karnataka | Karnataka | 1.00 |
| {"city": "Noida", "pincode": "201301"} | Uttar Pradesh | Uttar Pradesh | 1.00 |
| {"city": "Gurugram"} | Haryana | Haryana | 1.00 |
| {"city": "Navi Mumbai"} | Maharashtra | Maharashtra | 1.00 |
| {"city": "Kochi"} | Kerala | Kerala | 1.00 |
| {"city": "Visakhapatnam"} | Andhra Pradesh | Andhra Pradesh | 1.00 |
| {"city": "Hubballi"} | Karnataka | Karnataka | 1.00 |
| {"city": "Coimbatore"} | Tamil Nadu | Tamil Nadu | 1.00 |
| {"city": "Guwahati"} | Assam | Assam | 1.00 |
| {"pincode": "682001"} | Kerala | Kerala | 1.00 |
| {"pincode": "110045"} | Delhi | Delhi | 0.99 |
| {"city": "Vadodara"} | Gujarat | Gujarat | 1.00 |
| {"city": "Durgapur"} | West Bengal | West Bengal | 1.00 |
| {"city": "Bhubaneswar"} | Odisha | Odisha | 1.00 |
| {"city": "Mangaluru"} | Karnataka | Karnataka | 1.00 |
| {"city": "Madurai"} | Tamil Nadu | Tamil Nadu | 1.00 |
| {"city": "Nizamabad"} | Telangana | Telangana | 0.99 |
| {"city": "Tirupati"} | Andhra Pradesh | Andhra Pradesh | 1.00 |
| {"address": "MG Road"} | unknown | unknown | 0.50 |
| {"city": "Pondicherry"} | Puducherry | Puducherry | 1.00 |

## Task: Indian state from lead payload (jev-latest, run 2)

Accuracy: **21/22 = 95%**.

| payload | gold | predicted | conf |
|---|---|---|---|
| {"city": "Secunderabad", "pincode": "500003"} | Telangana | Telangana | 0.99 |
| {"address": "Flat 4B, Whitefield, Bengaluru 560066"} | Karnataka | Karnataka | 1.00 |
| {"city": "Belagavi"} | Karnataka | Karnataka | 1.00 |
| {"city": "Noida", "pincode": "201301"} | Uttar Pradesh | Uttar Pradesh | 1.00 |
| {"city": "Gurugram"} | Haryana | Haryana | 1.00 |
| {"city": "Navi Mumbai"} | Maharashtra | Maharashtra | 1.00 |
| {"city": "Kochi"} | Kerala | Kerala | 1.00 |
| {"city": "Visakhapatnam"} | Andhra Pradesh | Andhra Pradesh | 1.00 |
| {"city": "Hubballi"} | Karnataka | Karnataka | 1.00 |
| {"city": "Coimbatore"} | Tamil Nadu | Tamil Nadu | 1.00 |
| {"city": "Guwahati"} | Assam | Assam | 1.00 |
| {"pincode": "682001"} | Kerala | Kerala | 1.00 |
| {"pincode": "110045"} | Delhi | Delhi | 1.00 |
| {"city": "Vadodara"} | Gujarat | Gujarat | 1.00 |
| {"city": "Durgapur"} | West Bengal | West Bengal | 1.00 |
| {"city": "Bhubaneswar"} | Odisha | Odisha | 1.00 |
| {"city": "Mangaluru"} | Karnataka | Karnataka | 1.00 |
| {"city": "Madurai"} | Tamil Nadu | Tamil Nadu | 1.00 |
| {"city": "Nizamabad"} | Telangana | Telangana | 0.99 |
| {"city": "Tirupati"} | Andhra Pradesh | Andhra Pradesh | 1.00 |
| {"address": "MG Road"} | unknown | Karnataka **MISS** | 0.48 |
| {"city": "Pondicherry"} | Puducherry | Puducherry | 1.00 |

## Task: Indian state from lead payload (jev-preview, run 1)

Accuracy: **22/22 = 100%**.

| payload | gold | predicted | conf |
|---|---|---|---|
| {"city": "Secunderabad", "pincode": "500003"} | Telangana | Telangana | 0.99 |
| {"address": "Flat 4B, Whitefield, Bengaluru 560066"} | Karnataka | Karnataka | 1.00 |
| {"city": "Belagavi"} | Karnataka | Karnataka | 1.00 |
| {"city": "Noida", "pincode": "201301"} | Uttar Pradesh | Uttar Pradesh | 1.00 |
| {"city": "Gurugram"} | Haryana | Haryana | 1.00 |
| {"city": "Navi Mumbai"} | Maharashtra | Maharashtra | 1.00 |
| {"city": "Kochi"} | Kerala | Kerala | 1.00 |
| {"city": "Visakhapatnam"} | Andhra Pradesh | Andhra Pradesh | 1.00 |
| {"city": "Hubballi"} | Karnataka | Karnataka | 1.00 |
| {"city": "Coimbatore"} | Tamil Nadu | Tamil Nadu | 1.00 |
| {"city": "Guwahati"} | Assam | Assam | 1.00 |
| {"pincode": "682001"} | Kerala | Kerala | 1.00 |
| {"pincode": "110045"} | Delhi | Delhi | 1.00 |
| {"city": "Vadodara"} | Gujarat | Gujarat | 1.00 |
| {"city": "Durgapur"} | West Bengal | West Bengal | 1.00 |
| {"city": "Bhubaneswar"} | Odisha | Odisha | 1.00 |
| {"city": "Mangaluru"} | Karnataka | Karnataka | 1.00 |
| {"city": "Madurai"} | Tamil Nadu | Tamil Nadu | 1.00 |
| {"city": "Nizamabad"} | Telangana | Telangana | 0.99 |
| {"city": "Tirupati"} | Andhra Pradesh | Andhra Pradesh | 0.99 |
| {"address": "MG Road"} | unknown | unknown | 0.48 |
| {"city": "Pondicherry"} | Puducherry | Puducherry | 1.00 |
