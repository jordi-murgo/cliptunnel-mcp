import subprocess, os
os.chdir(os.path.dirname(os.path.abspath(__file__)))
msg = """fix: CI test failures in transport contracts (persist_seq, seq numbers, aes_key)

Three ControllerSlotContracts tests failed because:
- persist_seq parameter was removed from Controller.__init__ but tests
  still passed it (TypeError)
- Tests assumed seq=1 was consumed by a startup announce, but the
  Controller no longer auto-announces, so first command is seq=1 not
  seq=2

One AgentSlotContracts test failed because unpack() was called without
aes_key when CLIPTUNNEL_AES_KEY is set in the environment, causing the
encrypted payload to be returned as raw base64 instead of decrypted
JSON (JSONDecodeError on empty/ciphertext payload)."""
r = subprocess.run(["git", "add", "-A"], capture_output=True, text=True)
print("add:", r.returncode)
r = subprocess.run(["git", "commit", "-m", msg], capture_output=True, text=True)
print(r.stdout)
print(r.stderr)
print("rc:", r.returncode)
if r.returncode == 0:
    r2 = subprocess.run(["git", "push"], capture_output=True, text=True)
    print(r2.stdout)
    print(r2.stderr)
    print("push rc:", r2.returncode)