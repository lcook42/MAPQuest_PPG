"""The Arduino sketch is the single firmware source."""
from pathlib import Path

def sync(project):
    project = Path(project)
    source = project.parent / 'Arduino/MAX30101_ManualCuff_RTF/MAX30101_ManualCuff_RTF.ino'
    target = project / 'src/main.cpp'
    data = source.read_bytes()
    if not target.exists() or target.read_bytes() != data:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

try:
    Import('env')
except NameError:
    if __name__ == '__main__':
        sync(Path(__file__).resolve().parents[1])
else:
    sync(env.subst('$PROJECT_DIR'))
