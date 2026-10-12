"""The interactive host must never apply changes or mutate workspace files."""
from pathlib import Path

def test_interactive_host_has_no_source_mutation_calls():
    root = Path(__file__).resolve().parents[1] / "roslyn-host"
    offline_writers = {"Indexer.cs", "Bench.cs"}
    violations = []
    for source in root.glob("*.cs"):
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith(("//", "*", "/*")):
                continue
            for forbidden in ("TryApplyChanges", "File.WriteAllText", "File.Delete",
                              "File.Move", "Directory.Delete", "File.Create"):
                if (forbidden == "TryApplyChanges" or source.name not in offline_writers) and forbidden in line:
                    violations.append((source.name, number, forbidden))
    assert not violations
