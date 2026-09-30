import json
import os
from pydub import AudioSegment
from vosk import KaldiRecognizer, Model

# ১. ফাইল পথ এবং মডেল লোড
MODEL_PATH = "model"
MP3_FILE = "audio.mp3"  # আপনার MP3 ফাইলের নাম
WAV_TEMP_FILE = "temp_converted.wav"

if not os.path.exists(MODEL_PATH):
    print("ভুল: 'model' ফোল্ডার পাওয়া যায়নি!")
    exit(1)

if not os.path.exists(MP3_FILE):
    print(f"ভুল: '{MP3_FILE}' ফাইলটি পাওয়া যায়নি! সঠিক নাম দিন।")
    exit(1)

print("১/৩: Vosk মডেল লোড হচ্ছে...")
model = Model(MODEL_PATH)

print("২/৩: MP3 অডিও ফাইল প্রস্তুত করা হচ্ছে (16kHz Mono WAV)...")
# MP3 কে 16kHz Mono WAV ফরম্যাটে কনভার্ট করা
sound = AudioSegment.from_mp3(MP3_FILE)
sound = sound.set_channels(1)  # Mono channel
sound = sound.set_frame_rate(16000)  # 16kHz sample rate
sound.export(WAV_TEMP_FILE, format="wav")

print("৩/৩: অডিও ফাইল থেকে টেক্সট তৈরি করা হচ্ছে...")
recognizer = KaldiRecognizer(model, 16000)
results = []

with open(WAV_TEMP_FILE, "rb") as wf:
    while True:
        data = wf.read(32000)
        if len(data) == 0:
            break
        if recognizer.AcceptWaveform(data):
            part = json.loads(recognizer.Result())
            if part.get("text"):
                results.append(part["text"])

# সর্বশেষ ফলাফল সংগ্রহ
final_part = json.loads(recognizer.FinalResult())
if final_part.get("text"):
    results.append(final_part["text"])

# সম্পূর্ণ টেক্সট প্রিন্ট
full_text = " ".join(results)
print("\n" + "=" * 40)
print("সম্পূর্ণ টেক্সট (Transcription):")
print("=" * 40)
print(full_text if full_text else "কোনো কথা শনাক্ত করা যায়নি।")

# অস্থায়ী তৈরি হওয়া WAV ফাইলটি মুছে ফেলা
if os.path.exists(WAV_TEMP_FILE):
    os.remove(WAV_TEMP_FILE)

# আউটপুট টেক্সট ফাইলে সেভ করা
with open("output_text.txt", "w", encoding="utf-8") as text_file:
  text_file.write(full_text)

print("\nটেক্সটটি 'output_text.txt' ফাইলে সফলভাবে সেভ হয়েছে!")