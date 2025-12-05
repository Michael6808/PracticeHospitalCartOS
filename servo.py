from machine import Pin, PWM
import time
import sys
import select

servo_pins = [0, 1, 2, 3, 4]  # 5 servos
servos = [PWM(Pin(p)) for p in servo_pins]

for s in servos:
    s.freq(50)

led = Pin("LED", Pin.OUT)

# -------------------------------
# Helper functions
# -------------------------------

def servo_stop(idx):
    """Stop servo rotation (1.5ms pulse)."""
    servos[idx].duty_u16(duty_us_to_u16(1500))

def duty_us_to_u16(us):
    """Convert microseconds to 16-bit duty cycle at 50Hz (20ms period)."""
    return int((us / 20000) * 65535)

def spin_servo(idx, direction, duration=1):
    """Spin continuous servo at fixed speed for unlock/lock."""
    if direction == "unlock":
        # Spin one direction (e.g. clockwise)
        servos[idx].duty_u16(duty_us_to_u16(500))
    elif direction == "lock":
        # Spin other direction (e.g. counterclockwise)
        servos[idx].duty_u16(duty_us_to_u16(1980))
    else:
        servo_stop(idx)
        return

    time.sleep(duration)
    servo_stop(idx)

def unlock_servo(idx):
    print(f"Unlocking cabinet {idx+1}")
    spin_servo(idx, "unlock", duration=1)

def lock_servo(idx):
    print(f"Locking cabinet {idx+1}")
    spin_servo(idx, "lock", duration=1)

def flash_led(times=3):
    for _ in range(times):
        led.on()
        time.sleep(0.2)
        led.off()
        time.sleep(0.2)

# -------------------------------
# Main setup
# -------------------------------
print("Pico continuous servo controller ready...")
flash_led(3)

buffer = ""
last_unlock_time = [None] * len(servos)
AUTOLOCK_DELAY = 20

while True:
    # --- read serial ---
    if sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
        data = sys.stdin.read(1)
        if data == "\n":
            cmd = buffer.strip().upper()
            buffer = ""

            if cmd.startswith("UNLOCK"):
                try:
                    num = int(cmd[-1]) - 1
                    unlock_servo(num)
                    last_unlock_time[num] = time.time()
                except Exception as e:
                    print("Invalid unlock command:", e)

            elif cmd.startswith("LOCK"):
                try:
                    num = int(cmd[-1]) - 1
                    lock_servo(num)
                    last_unlock_time[num] = None
                except Exception as e:
                    print("Invalid lock command:", e)

            elif cmd == "HELLO":
                print("HELLO from Pico")
                flash_led(2)

            else:
                print("Unknown command:", cmd)
        else:
            buffer += data

    # --- auto-lock check ---
    now = time.time()
    for i in range(len(servos)):
        if last_unlock_time[i] and now - last_unlock_time[i] > AUTOLOCK_DELAY:
            print(f"Auto-locking cabinet {i+1}")
            lock_servo(i)
            last_unlock_time[i] = None

    time.sleep(0.1)
