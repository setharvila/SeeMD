# SeeMD

A simple, local speech-to-text display for a Music Director's (MD) microphone, so an audio engineer can read what's being said without wearing headphones. Runs fully offline — no cloud services, no subscriptions.

Transcription prioritizes **speed over accuracy**: text appears live, word by word, as the MD speaks.

## Features

- Live, word-by-word transcription (no waiting for a pause to see text)
- Chat-style transcript grid: local clock time + "time ago" on the left, message on the right, newest at the bottom
- Speech is grouped into chunks, starting a new row after ~2 seconds of silence
- Color-coded by age: white (<1 min), yellow (1–3 min), red (3+ min)
- Auto-clears transcript rows older than 10 minutes
- Editable title field, useful for telling multiple running instances apart (e.g. separate mics/feeds)
- Mic selector, adjustable font size, collapsible toolbar, "stay on top" toggle
- Live CPU usage meter with an optional soft CPU cap (self-throttles if this shouldn't hog the machine)

## Requirements

- Python 3.9+
- A Vosk speech model (small English model recommended for speed)

## Setup

1. Install dependencies:

   ```
   pip install -r requirements.txt
   ```

2. Download a [Vosk model](https://alphacephei.com/vosk/models) and unzip it into a folder named `model` in the project root. The small English model works well for this use case:

   ```
   curl -L -o vosk-model.zip https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
   unzip vosk-model.zip
   mv vosk-model-small-en-us-0.15 model
   rm vosk-model.zip
   ```

3. Run it:

   ```
   python3 main.py
   ```

## Multiple speakers / feeds

This model doesn't do speaker diarization. For multiple speakers, run one instance per microphone, and use the editable title field at the top of each window to label which feed is which (e.g. "MD", "Vocalist 2").

## License

Third-party components used: [Vosk](https://github.com/alphacep/vosk-api) (Apache 2.0), [sounddevice](https://github.com/spatialaudio/python-sounddevice) (MIT), [psutil](https://github.com/giampaolo/psutil) (BSD).
