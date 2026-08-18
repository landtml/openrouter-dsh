#!/usr/bin/env python3
"""merge_settings.py -- merge the OpenRouter fragment into ~/.dsh/settings.yaml.

    python3 merge_settings.py <settings.yaml> <fragment.yaml>

WHY THIS IS NOT A NAIVE TEXT SPLICE
-----------------------------------
Every dsh provider lives nested inside the single top-level key `llm-pi-ai`:

    llm-pi-ai:
      providers:
        anthropic:   ...
        openrouter:  ...     <- the only one this fragment owns

So replacing the whole `llm-pi-ai:` block -- the obvious text merge -- deletes
every OTHER provider the user configured. That is real data loss, and it is
the common case for anyone running more than one model.

This merges at the level the fragment actually owns:

  * `llm-pi-ai.providers.<routeKey>`  -- replaced; sibling providers untouched
  * any other top-level key in the fragment -- replaced wholesale
  * everything else -- left exactly as it was

Comments in the FRAGMENT are preserved verbatim, because in this repo the
comments are the deliverable -- they record why each value is what it is.
Comments in the user's own file are preserved for every part of the tree this
does not rewrite; comments inside a replaced provider block are necessarily
lost, and the tool says so.

PyYAML is required only when the existing file is non-trivial. A fresh install
(no settings.yaml, or one with no `llm-pi-ai` key) needs no dependencies.
"""
import os
import re
import shutil
import sys


def top_level_blocks(text):
    """Split YAML into {key: block_text} for top-level keys, plus their order.

    A top-level key is a line matching `^([A-Za-z0-9_.-]+):` at column 0. Its
    block runs until the next such line. Comments immediately above a key
    travel with that key.
    """
    lines = text.splitlines(keepends=True)
    blocks, order = {}, []
    cur_key, cur, pending = None, [], []

    for line in lines:
        m = re.match(r"^([A-Za-z0-9_.-]+):", line)
        if m:
            if cur_key is not None:
                blocks.setdefault(cur_key, "")
                blocks[cur_key] += "".join(cur)
            elif cur:
                blocks["__preamble__"] = "".join(cur)
                order.append("__preamble__")
            cur_key = m.group(1)
            order.append(cur_key)
            cur = pending + [line]
            pending = []
        elif cur_key is None and (line.strip().startswith("#") or not line.strip()):
            pending.append(line)
        else:
            if pending:
                cur.extend(pending)
                pending = []
            cur.append(line)

    if cur_key is not None:
        blocks.setdefault(cur_key, "")
        blocks[cur_key] += "".join(cur) + "".join(pending)
    elif cur or pending:
        blocks["__preamble__"] = "".join(cur + pending)
        if "__preamble__" not in order:
            order.append("__preamble__")

    return blocks, order


def indent_of(line):
    return len(line) - len(line.lstrip(" "))


def replace_provider(existing_block, route_key, fragment_block):
    """Swap one provider inside an existing `llm-pi-ai:` block.

    Returns (new_block, note) or (None, reason) if the shape is not what we
    can safely edit -- in which case the caller falls back to a full parse.
    """
    lines = existing_block.splitlines(keepends=True)

    # Locate `  providers:` (any indent > 0) inside this block.
    prov_i = None
    for i, line in enumerate(lines):
        if re.match(r"^\s+providers:\s*$", line):
            prov_i = i
            break
    if prov_i is None:
        return None, "no `providers:` mapping found"

    prov_indent = indent_of(lines[prov_i])

    # Find the child keys of providers:.
    child_indent = None
    entries = []          # (key, start, end)
    i = prov_i + 1
    cur_key, cur_start = None, None
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.strip().startswith("#"):
            i += 1
            continue
        ind = indent_of(line)
        if ind <= prov_indent:
            break                       # left the providers mapping
        if child_indent is None:
            child_indent = ind
        if ind == child_indent:
            m = re.match(r"^\s+([A-Za-z0-9_.-]+):", line)
            if m:
                if cur_key is not None:
                    entries.append((cur_key, cur_start, i))
                cur_key, cur_start = m.group(1), i
        i += 1
    if cur_key is not None:
        entries.append((cur_key, cur_start, i))

    if not entries:
        return None, "`providers:` mapping is empty"

    # Re-indent the fragment's provider body to match this file.
    frag_lines = fragment_block.splitlines(keepends=True)
    fprov_i = None
    for j, line in enumerate(frag_lines):
        if re.match(r"^\s+providers:\s*$", line):
            fprov_i = j
            break
    if fprov_i is None:
        return None, "fragment has no `providers:` mapping"

    frag_body = frag_lines[fprov_i + 1:]
    while frag_body and not frag_body[-1].strip():
        frag_body.pop()
    if not frag_body:
        return None, "fragment provider body is empty"

    # The fragment's child indent is the indent of its FIRST provider key,
    # not the min over the whole body: comment lines and nested values sit at
    # other depths, and a stray comment at column 0 would poison a min().
    frag_child_indent = None
    for l in frag_body:
        if l.strip() and not l.lstrip().startswith("#"):
            frag_child_indent = indent_of(l)
            break
    if frag_child_indent is None:
        return None, "fragment provider body has no content" 
    shift = child_indent - frag_child_indent
    if shift > 0:
        frag_body = [(" " * shift + l) if l.strip() else l for l in frag_body]
    elif shift < 0:
        cut = -shift
        out = []
        for l in frag_body:
            if not l.strip():
                out.append(l)
            elif l[:cut].strip() == "":
                out.append(l[cut:])
            else:
                return None, "cannot re-indent fragment safely"
        frag_body = out

    existing_keys = [k for k, _, _ in entries]
    if route_key in existing_keys:
        s, e = next((s, e) for k, s, e in entries if k == route_key)
        new = lines[:s] + frag_body + lines[e:]
        note = f"replaced provider {route_key!r}"
    else:
        _, _, e = entries[-1]
        new = lines[:e] + frag_body + lines[e:]
        note = f"added provider {route_key!r}"

    kept = [k for k in existing_keys if k != route_key]
    return "".join(new), note + (
        f"; kept {', '.join(kept)}" if kept else "")


def fragment_route_key(fragment):
    """The provider key the fragment defines, e.g. 'openrouter'."""
    blocks, _ = top_level_blocks(fragment)
    blk = blocks.get("llm-pi-ai")
    if not blk:
        return None
    lines = blk.splitlines()
    prov_i = None
    for i, line in enumerate(lines):
        if re.match(r"^\s+providers:\s*$", line):
            prov_i = i
            break
    if prov_i is None:
        return None
    child_indent = None
    for line in lines[prov_i + 1:]:
        if not line.strip() or line.strip().startswith("#"):
            continue
        ind = indent_of(line)
        if child_indent is None:
            child_indent = ind
        if ind == child_indent:
            m = re.match(r"^\s+([A-Za-z0-9_.-]+):", line)
            if m:
                return m.group(1)
        if ind < (child_indent or 0):
            break
    return None


def validate(text, frag_keys, route_key=None):
    """Structural checks. Returns (ok, message).

    Only the provider the FRAGMENT owns is checked -- the user's other
    providers are none of this tool's business and will not have an
    openRouterRouting block.
    """
    try:
        import yaml
    except ImportError:
        return True, "PyYAML not installed -- skipped structural validation"
    try:
        parsed = yaml.safe_load(text)
    except Exception as e:                                   # noqa: BLE001
        return False, f"merged result is not valid YAML: {e}"
    if not isinstance(parsed, dict):
        return False, "merged result is not a YAML mapping"
    for key in frag_keys:
        if key not in parsed:
            return False, f"merged result lost expected key {key!r}"
    try:
        provs = parsed["llm-pi-ai"]["providers"]
        route = route_key if route_key in provs else next(
            k for k in provs
            if (provs[k].get("models") or [{}])[0].get("compat", {})
            .get("openRouterRouting"))
        m = provs[route]["models"][0]
        assert m["compat"]["openRouterRouting"]["order"], "empty routing order"
        assert m["reasoningEfforts"], "empty reasoningEfforts"
        if m["reasoningEfforts"].get("off") is False:
            return False, ('reasoningEfforts.off parsed as boolean false. '
                           'In YAML 1.1 a bare `off` is a boolean -- it must '
                           'be quoted: "off": none')
    except (KeyError, IndexError, TypeError, StopIteration, AssertionError) as e:
        return False, f"merged result failed a structural check: {e}"
    return True, "validated with PyYAML"


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

    route_key = fragment_route_key(fragment)

    existing = ""
    if os.path.exists(settings_path):
        with open(settings_path, encoding="utf-8") as f:
            existing = f.read()

    notes = []

    if not existing.strip():
        result = fragment
        notes.append("fresh settings file")
    else:
        cur_blocks, cur_order = top_level_blocks(existing)

        # A key repeated at top level means the file is already malformed;
        # replacing it once per occurrence would duplicate the fragment.
        seen, dupes = set(), []
        for k in cur_order:
            if k in seen and k != "__preamble__":
                dupes.append(k)
            seen.add(k)
        if dupes:
            print(f"ERROR: {settings_path} has duplicate top-level key(s): "
                  f"{', '.join(sorted(set(dupes)))}.\n"
                  f"       That file is already ambiguous (YAML takes the last "
                  f"one). Merge them by hand first, then re-run.",
                  file=sys.stderr)
            return 1

        out, replaced, kept = [], [], []
        emitted = set()

        for key in cur_order:
            if key == "__preamble__":
                out.append(cur_blocks[key])
                continue

            if key == "llm-pi-ai" and route_key and "llm-pi-ai" in frag_blocks:
                merged, note = replace_provider(
                    cur_blocks[key], route_key, frag_blocks["llm-pi-ai"])
                if merged is not None:
                    out.append(merged)
                    replaced.append(f"llm-pi-ai ({note})")
                    emitted.add("llm-pi-ai")
                    continue
                # Could not edit surgically -- fall back, but say what is lost.
                others = re.findall(r"^\s{4,6}([A-Za-z0-9_.-]+):",
                                    cur_blocks[key], re.M)
                others = [o for o in others if o not in
                          ("providers", "models", "compat", route_key)]
                print(f"WARNING: could not merge into your existing "
                      f"`llm-pi-ai` block ({note}); replacing it wholesale."
                      + (f"\n         This DELETES: {', '.join(sorted(set(others)))}"
                         if others else ""),
                      file=sys.stderr)
                out.append(frag_blocks["llm-pi-ai"])
                replaced.append("llm-pi-ai (wholesale)")
                emitted.add("llm-pi-ai")
                continue

            if key in frag_blocks:
                out.append(frag_blocks[key])
                replaced.append(key)
                emitted.add(key)
            else:
                out.append(cur_blocks[key])
                kept.append(key)

        for key in frag_keys:
            if key not in emitted:
                if out and not out[-1].endswith("\n\n"):
                    out.append("\n")
                out.append(frag_blocks[key])
                replaced.append(f"{key} (added)")

        result = "".join(out)
        notes.append(f"replaced: {', '.join(replaced) or '(none)'}")
        notes.append(f"preserved: {', '.join(kept) or '(none)'}")

    ok, msg = validate(result, frag_keys, route_key)
    if not ok:
        print(f"ERROR: {msg}", file=sys.stderr)
        print("       Your settings file was NOT modified.", file=sys.stderr)
        return 1

    tmp = settings_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(result)
    shutil.move(tmp, settings_path)

    for n in notes:
        print(f"    {n}")
    print(f"    wrote {settings_path} ({msg})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
