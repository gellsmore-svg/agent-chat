import os
import shutil
import tempfile
from pathlib import Path

from .protocol import BACKENDS, ProtocolError, checked, is_checkout

ASSET = Path("skill", "agent-chat")
SKILL = Path(__file__).resolve().parent / ASSET
DISCOVERY = {"codex": ".agents", "grok": ".grok", "claude": ".claude"}
CUSTOM_HOME = {"grok": "GROK_HOME", "claude": "CLAUDE_CONFIG_DIR"}


def home_skills(name):
    variable = CUSTOM_HOME.get(name)
    configured = os.environ.get(variable) if variable else None
    return (Path(configured).expanduser() if configured else Path.home() / DISCOVERY[name]) / "skills"


def copy_matches(destination):
    return (
        not destination.is_symlink() and destination.is_dir()
        and {entry.name for entry in destination.iterdir()} == {"SKILL.md"}
        and not (destination / "SKILL.md").is_symlink()
        and (destination / "SKILL.md").is_file()
        and (destination / "SKILL.md").read_bytes() == (SKILL / "SKILL.md").read_bytes()
    )


def install(args):
    root = checked(args.root, "dir") if args.root else None
    for name in args.backend or sorted(BACKENDS):
        base = checked(root / DISCOVERY[name] / "skills" if root else home_skills(name))
        base.mkdir(parents=True, exist_ok=True)
        destination = base / SKILL.name
        previous = None
        if destination.is_symlink():
            target = destination.resolve()
            if target == SKILL or (
                is_checkout(target) and (target / "src" / "agent_chat" / ASSET / "SKILL.md").is_file()
            ):
                if target == SKILL and not args.copy:
                    print(f"{name}: already installed at {destination}")
                    continue
                previous = destination.readlink()
            else:
                raise ProtocolError(f"Installation path already exists: {destination}")
        elif destination.exists():
            if copy_matches(destination):
                print(f"{name}: already installed at {destination}")
                continue
            raise ProtocolError(f"Installation path already exists: {destination}")
        with tempfile.TemporaryDirectory(prefix=".agent-chat-install-", dir=base) as directory:
            staged = Path(directory) / SKILL.name
            linked = False
            if not args.copy:
                try:
                    staged.symlink_to(SKILL, target_is_directory=True)
                    linked = True
                except (NotImplementedError, OSError):
                    pass
            if not linked:
                staged.mkdir()
                shutil.copyfile(SKILL / "SKILL.md", staged / "SKILL.md")
            if previous:
                destination.unlink()
            try:
                staged.rename(destination)
            except OSError:
                if previous and not destination.exists() and not destination.is_symlink():
                    destination.symlink_to(previous, target_is_directory=True)
                raise
        print(f"{name}: {'linked' if linked else 'copied'} {destination}")
    return 0
