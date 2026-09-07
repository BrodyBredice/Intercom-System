from gpiozero import Button, RGBLED, Buzzer
import os
import signal
import socket
import struct
import subprocess
import threading
import time

# ---------------- SETTINGS ----------------

MY_NAME = "Intercom A"
TARGET_NAME = "Intercom B"
TARGET_IP = "192.168.1.119"

PORT = 5000

BUTTON_PIN = 17
LED_RED_PIN = 27
LED_GREEN_PIN = 23
LED_BLUE_PIN = 24
BUZZER_PIN = 22

OUTGOING_FILE = "/tmp/intercom_outgoing.wav"
INCOMING_TEMP_FILE = "/tmp/intercom_incoming.tmp"
INCOMING_FILE = "/tmp/intercom_incoming.wav"

SAMPLE_RATE = 48000
CHANNELS = 2

# Set this to the device that works on this Pi.
# Examples: "plughw:0,0" or "plughw:1,0"
AUDIO_DEVICE = "plughw:0,0"

# ------------------------------------------

button = Button(BUTTON_PIN, pull_up=True, bounce_time=0.05)
led = RGBLED(
    red=LED_RED_PIN,
    green=LED_GREEN_PIN,
    blue=LED_BLUE_PIN,
    active_high=True
)

def led_ready():
    led.color = (0, 1, 0)      # Green

def led_recording():
    led.color = (1, 0, 0)      # Red

def led_receiving():
    led.color = (0, 0, 1)      # Blue

def led_off():
    led.off()

buzzer = Buzzer(BUZZER_PIN)

led_ready()

audio_lock = threading.Lock()
stop_event = threading.Event()


def beep(duration=0.75, count=1, pause=0.1):
    for _ in range(count):
        buzzer.on()
        time.sleep(duration)
        buzzer.off()
        time.sleep(pause)


def remove_file(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def record_message():
    remove_file(OUTGOING_FILE)

    print("Recording...")
    led_recording()

    process = subprocess.Popen([
        "arecord",
        "-D", AUDIO_DEVICE,
        "-f", "S16_LE",
        "-r", str(SAMPLE_RATE),
        "-c", str(CHANNELS),
        "-t", "wav",
        OUTGOING_FILE
    ])

    button.wait_for_release()

    process.send_signal(signal.SIGINT)
    return_code = process.wait()
    led_ready()

    time.sleep(0.25)

    if return_code not in (0, 1, 130):
        print(f"Recording command failed with code {return_code}.")
        return False

    if not os.path.exists(OUTGOING_FILE):
        print("No outgoing audio file was created.")
        return False

    file_size = os.path.getsize(OUTGOING_FILE)

    if file_size <= 1000:
        print(f"Recording is too small: {file_size} bytes.")
        return False

    print(f"Recorded {file_size} bytes.")
    return True


def send_message():
    file_size = os.path.getsize(OUTGOING_FILE)

    try:
        with socket.create_connection((TARGET_IP, PORT), timeout=10) as sock:
            sock.sendall(struct.pack("!Q", file_size))

            with open(OUTGOING_FILE, "rb") as audio_file:
                while True:
                    chunk = audio_file.read(4096)
                    if not chunk:
                        break
                    sock.sendall(chunk)

        print(f"Sent message to {TARGET_NAME}.")
        beep(duration=0.08, count=2, pause=0.08)

    except Exception as error:
        print(f"Send failed: {error}")
        beep(duration=0.4, count=1)


def sender_loop():
    print(f"{MY_NAME} sender ready.")

    while not stop_event.is_set():
        button.wait_for_press()

        if stop_event.is_set():
            break

        with audio_lock:
            if record_message():
                send_message()


def receive_exactly(connection, byte_count):
    data = bytearray()

    while len(data) < byte_count:
        chunk = connection.recv(byte_count - len(data))

        if not chunk:
            raise ConnectionError("Connection closed before header completed.")

        data.extend(chunk)

    return bytes(data)


def receive_message(connection):
    remove_file(INCOMING_TEMP_FILE)
    remove_file(INCOMING_FILE)

    header = receive_exactly(connection, 8)
    expected_size = struct.unpack("!Q", header)[0]

    if expected_size <= 0 or expected_size > 100_000_000:
        raise ValueError(f"Invalid incoming file size: {expected_size}")

    received_size = 0

    with open(INCOMING_TEMP_FILE, "wb") as audio_file:
        while received_size < expected_size:
            chunk = connection.recv(min(4096, expected_size - received_size))

            if not chunk:
                break

            audio_file.write(chunk)
            received_size += len(chunk)

        audio_file.flush()
        os.fsync(audio_file.fileno())

    if received_size != expected_size:
        remove_file(INCOMING_TEMP_FILE)
        raise ConnectionError(
            f"Incomplete transfer: expected {expected_size}, got {received_size}"
        )

    os.replace(INCOMING_TEMP_FILE, INCOMING_FILE)

    actual_size = os.path.getsize(INCOMING_FILE)

    if actual_size != expected_size:
        remove_file(INCOMING_FILE)
        raise IOError(
            f"Saved file size mismatch: expected {expected_size}, got {actual_size}"
        )

    print(f"Received a new message: {actual_size} bytes.")


def play_message():
    print("Playing new message...")
    led_receiving()

    result = subprocess.run([
        "aplay",
        "-D", AUDIO_DEVICE,
        INCOMING_FILE
    ])

    led_ready()

    if result.returncode != 0:
        print(f"Playback failed with code {result.returncode}.")
    else:
        print("Playback finished.")


def receiver_loop():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", PORT))
    server.listen(5)
    server.settimeout(1)

    print(f"{MY_NAME} receiver listening on port {PORT}.")

    while not stop_event.is_set():
        try:
            connection, address = server.accept()
        except socket.timeout:
            continue

        try:
            with connection:
                print(f"Incoming connection from {address[0]}.")
                receive_message(connection)

            with audio_lock:
                beep(duration=0.75, count=1)
                play_message()

        except Exception as error:
            print(f"Receive/playback error: {error}")
            remove_file(INCOMING_TEMP_FILE)

    server.close()


receiver_thread = threading.Thread(target=receiver_loop, daemon=True)
receiver_thread.start()

try:
    sender_loop()

except KeyboardInterrupt:
    print("\nStopping intercom...")

finally:
    stop_event.set()
    led.off()
    buzzer.off()
