import keyboard
import time
import threading

is_typing = False
hooked = False

def blocker(event):
    if event.name in ['ctrl', 'left ctrl', 'right ctrl'] and event.event_type == 'down':
        global is_typing
        print("Ctrl pressed, stopping!")
        is_typing = False
        return True
    
    if event.name in ['ctrl', 'left ctrl', 'right ctrl', 'shift', 'left shift', 'right shift', 'q', 'p', 'Q', 'P']:
        return True
    print(f"Blocked {event.name}")
    return False

def on_toggle():
    global is_typing
    is_typing = not is_typing
    print(f"Toggle! is_typing={is_typing}")

keyboard.add_hotkey('ctrl+p', on_toggle, suppress=True)

def worker():
    global is_typing, hooked
    while True:
        if is_typing and not hooked:
            keyboard.hook(blocker, suppress=True)
            hooked = True
            print("HOOKED")
        elif not is_typing and hooked:
            keyboard.unhook(blocker)
            hooked = False
            print("UNHOOKED")
        
        if is_typing:
            print("Typing...", flush=True)
            time.sleep(0.5)
        else:
            time.sleep(0.1)

t = threading.Thread(target=worker, daemon=True)
t.start()

time.sleep(10)
