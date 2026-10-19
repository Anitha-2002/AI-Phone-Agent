# Phone-quality test set (record 20-30 utterances, over a real phone call, 8 kHz)

Record each as a call to your Twilio number (or record your own phone's audio), then keep the
reference text next to the audio so you can compute WER later.

Cover at least:
- 5 plain requests in your use case ("I'd like to book an appointment for Friday")
- 3 with numbers, dates, times, names, emails read aloud
- 3 with corrections ("...tomorrow, no, I mean next week")
- 3 with long pauses or "um" mid-sentence
- 3 interruptions: talk over the agent early, mid, and late in its reply
- 3 short backchannels while the agent speaks ("uh-huh", "okay", a cough)
- 3 in a noisy place (street, car, speakerphone)
- 2 with an accent or speaking speed different from your own

Store files under `recordings/` (git-ignored). Do not commit real customer audio.
