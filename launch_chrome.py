import subprocess, sys, os

chrome_paths = [
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
]

chrome = None
for p in chrome_paths:
    if os.path.exists(p):
        chrome = p
        break

if not chrome:
    print("Chrome not found!")
    sys.exit(1)

print("Launching:", chrome)
subprocess.Popen(
    [chrome, "--remote-debugging-port=9222", "https://m829.crw066.com"],
    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    close_fds=True,
)
print("Chrome launched with CDP on :9222")
print("Login to HG site, then verify at http://localhost:9222/json/version")