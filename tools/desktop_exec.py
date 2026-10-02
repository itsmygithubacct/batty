# SPDX-License-Identifier: MIT
"""Desktop Entry Exec argument decoding, without shell evaluation."""
from pathlib import Path
from urllib.parse import unquote, urlsplit


def tokens(text):
    result = []
    i = 0
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        quoted = text[i] == '"'
        if quoted:
            i += 1
        word = []
        closed = not quoted
        while i < len(text):
            c = text[i]
            if quoted and c == '"':
                i += 1
                closed = True
                if i < len(text) and not text[i].isspace():
                    raise ValueError('Desktop Exec quoting must cover a whole argument')
                break
            if not quoted and c.isspace():
                break
            if c == '"':
                raise ValueError('Desktop Exec quoting must cover a whole argument')
            if c == '\\':
                i += 1
                if i == len(text) or (quoted and text[i] not in '\\"`$'):
                    raise ValueError('Invalid Desktop Exec argument escape')
                c = text[i]
            word.append(c)
            i += 1
        if not closed:
            raise ValueError('Unterminated Desktop Exec argument')
        result.append((''.join(word), quoted))
    return result


def file_argument(value, uri):
    parsed = urlsplit(value)
    if parsed.scheme:
        if uri:
            return value
        if parsed.scheme != 'file' or parsed.netloc not in ('', 'localhost') or parsed.query or parsed.fragment:
            raise ValueError('This application accepts local files, not remote URLs')
        value = unquote(parsed.path, errors='strict')
        if not Path(value).is_absolute():
            raise ValueError('File URLs must have an absolute path')
    path = Path(value).absolute()
    return path.as_uri() if uri else str(path)


def expand_exec(text, entry, inputs=()):
    """text has already undergone Desktop Entry string unescaping.

    Expansion follows tokenization; inserted values are never reparsed as code,
    quoting or further field codes. Singular file codes require one input.
    """
    words = tokens(text)
    if not words:
        raise ValueError('Application has an empty Exec command')
    result = []
    file_codes = 0
    for index, (word, quoted) in enumerate(words):
        out = []
        expanded = None
        i = 0
        removed = False
        while i < len(word):
            if word[i] != '%':
                out.append(word[i]); i += 1; continue
            i += 1
            if i == len(word):
                raise ValueError('Incomplete Desktop Exec field code')
            code = word[i]; i += 1
            if code == '%':
                out.append('%'); continue
            if quoted or index == 0:
                raise ValueError('Desktop Exec field codes cannot appear in quoted arguments or the executable')
            if code in 'fFuU':
                file_codes += 1
                if file_codes > 1 or word != '%' + code:
                    raise ValueError('Use one standalone Desktop Exec file or URL code')
                if code in 'fu' and len(inputs) > 1:
                    raise ValueError('This application accepts one file or URL per launch')
                expanded = [file_argument(value, code in 'uU') for value in inputs]
            elif code == 'i':
                if word != '%i':
                    raise ValueError('Desktop Exec %i must be a separate argument')
                expanded = ['--icon', entry['icon']] if entry.get('icon') else []
            elif code == 'c':
                out.append(entry['name'])
            elif code == 'k':
                out.append(entry['path'])
            elif code in 'dDnNvm':
                removed = True
            else:
                raise ValueError(f'Unsupported Desktop Exec field code: %{code}')
        if expanded is not None:
            result.extend(expanded)
        elif out or not removed:
            result.append(''.join(out))
    if inputs and not file_codes:
        raise ValueError('This application does not declare file or URL arguments')
    if not result or not result[0] or '=' in result[0] or any('\0' in arg for arg in result):
        raise ValueError('Application has an invalid Exec command')
    return result
