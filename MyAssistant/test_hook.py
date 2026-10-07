import keyboard
import time

def block(event):
    print(event.name)
    if event.name == 'a':
        return True # Try to allow
    return False # Try to block

keyboard.hook(block, suppress=True)
time.sleep(3)
keyboard.unhook_all()
