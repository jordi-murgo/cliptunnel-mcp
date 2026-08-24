import subprocess, os
os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.remove("_commit.py")
subprocess.run(["git", "add", "-A"])
subprocess.run(["git", "commit", "-m", "chore: remove temp commit script"])
subprocess.run(["git", "push"])