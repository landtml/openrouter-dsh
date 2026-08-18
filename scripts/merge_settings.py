#!/usr/bin/env python3
"""merge_settings.py -- merge the OpenRouter fragment into ~/.dsh/settings.yaml.

    python3 merge_settings.py <settings.yaml> <fragment.yaml>

Deliberately dependency-free. PyYAML is not in the stdlib and requiring a pip
install before a user can configure their harness is a bad trade, so this uses
a targeted TEXT merge rather than a parse-and-reserialize round trip.

That choice has a second, larger benefit: a YAML round trip would strip every
comment from the fragment, and in this file the comments ARE the deliverable.
They record why each value is what it is, and every one of those values has a
silent failure mode if set wrong.

Strategy: replace the top-level blocks the fragment defines, leave every other
block byte-identical. If PyYAML happens to be available it is used to VALIDATE
the result, never to produce it.
"""
import os
import re
import shutil
import sys


def top_level_blocks(text):
    """Split YAML into {key: block_text} for top-level keys only.

    A top-level key is a line matching `^([A-Za-z0-9_-]+):` at column 0. Its
    block runs until the next such line. Leading comments attached above a key
    travel with that key.
    """
    lines = text.splitlines(keepends=True)
    blocks = {}
    order = []
    cur_key = None
    cur = []
    pending_comments = []

    for line in lines:
        m = re.match(r"^([A-Za-z0-9_.-]+):", line)
        if m:
            if cur_key is not None:
                blocks[cur_key] = "".join(cur)
            else:
                if cur:
                    blocks["__preamble__"] = "".join(cur)
                    order.append("__preamble__")
            cur_key = m.group(1)
            order.append(cur_key)
            cur = pending_comments + [line]
            pending_comments = []
        elif cur_key is None and (line.strip().startswith("#") or not line.strip()):
            # Comments before the first key: preamble, unless they turn out to
            # be attached to the next key. Treat a comment run immediately
            # followed by a key as attached to that key.
            pending_comments.append(line)
        else:
            if pending_comments:
                cur.extend(pending_comments)
                pending_comments = []
            cur.append(line)

    if cur_key is not None:
        blocks[cur_key] = "".join(cur)
    elif cur or pending_comments:
        blocks["__preamble__"] = "".join(cur + pending_comments)
        if "__preamble__" not in order:
            order.append("__preamble__")

    if pending_comments and cur_key is not None:
        blocks[cur_key] = blocks[cur_key] + "".join(pending_comments)

    return blocks, order


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2

    settings_path, fragment_path = sys.argv[1], sys.argv[2]

    with open(fragment_path, encoding="utf-8") as f:
        fragment = f.read()

    frag_blocks, frag_order = top_level_blocks(fragment)
    frag_keys = [k for k in frag_order if k != "__preamble__"]
    if not frag_keys:
        print("ERROR: fragment defines no top-level keys", file=sys.stderr)
        return 1

    if os.path.exists(settings_path):
        with open(settings_path, encoding="utf-8") as f:
            existing = f.read()
    else:
        existing = ""

    if not existing.strip():
        # Fresh file: the fragment IS the settings.
        result = fragment
        replaced, kept = frag_keys, []
    else:
        cur_blocks, cur_order = top_level_blocks(existing)
        replaced, kept, out = [], [], []

        # Preserve original ordering; swap in fragment versions where they exist.
        for key in cur_order:
            if key == "__preamble__":
                out.append(cur_blocks[key])
            elif key in frag_blocks:
                out.append(frag_blocks[key])
                replaced.append(key)
            else:
                out.append(cur_blocks[key])
                kept.append(key)

        # Append fragment keys the existing file never had.
        for key in frag_keys:
            if key not in cur_blocks:
                if out and not out[-1].endswith("\n\n"):
                    out.append("\n")
                out.append(frag_blocks[key])
                replaced.append(key)

        result = "".join(out)

    # ------------------------------------------------------------- validate
    # Optional: if PyYAML is installed, confirm the merge produced valid YAML
    # with the keys we expect. Never used to REWRITE the file -- that would
    # strip the comments this repo exists to deliver.
    try:
        import yaml
        parsed = yaml.safe_load(result)
        if not isinstance(parsed, dict):
            print("ERROR: merged result is not a YAML mapping", file=sys.stderr)
            return 1
        for key in frag_keys:
            if key not in parsed:
                print(f"ERROR: merged result lost expected key {key!r}",
                      file=sys.stderr)
                return 1
        # Spot-check the load-bearing values survived.
        try:
            m = parsed["llm-pi-ai"]["providers"]["openrouter"]["models"][0]
            assert m["compat"]["openRouterRouting"]["order"], "empty order"
            assert m["reasoningEfforts"], "empty reasoningEfforts"
            off = m["reasoningEfforts"].get("off", "MISSING")
            if off is False:
                print("ERROR: reasoningEfforts.off parsed as boolean false.\n"
                      "       In YAML 1.1 a bare `off` is a boolean. It must "
                      "be quoted: \"off\": none", file=sys.stderr)
                return 1
        except (KeyError, IndexError, TypeError, AssertionError) as e:
            print(f"ERROR: merged result failed a structural check: {e}",
                  file=sys.stderr)
            return 1
        validated = " (validated with PyYAML)"
    except ImportError:
        validated = " (PyYAML not installed -- skipped structural validation)"
    except Exception as e:  # noqa: BLE001 - yaml can raise many parse errors
        print(f"ERROR: merged result is not valid YAML: {e}", file=sys.stderr)
        return 1

    # ---------------------------------------------------------- write atomically
    tmp = settings_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(result)
    shutil.move(tmp, settings_path)

    print(f"    replaced: {', '.join(replaced) or '(none)'}")
    print(f"    preserved: {', '.join(kept) or '(none)'}")
    print(f"    wrote {settings_path}{validated}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
