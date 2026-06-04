import asyncio
import re
import urllib.parse
from urllib.parse import quote
import hashlib
def check_platform_sync(url: str):
    patterns = {
        "tiktok": r"(https?://)?(www\.)?(vm\.)?tiktok\.com/",
        "instagram": r"(https?://)?(www\.)?instagram\.com/",
        "youtube": r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/",
        "facebook": r"(https?://)?(www\.)?(facebook\.com|fb\.watch)/"
    }

    for platform, pattern in patterns.items():
        if re.search(pattern, url):
            return platform
    return None


async def check_platform(url: str):
    return await asyncio.to_thread(check_platform_sync, url)


import asyncio

def generate_internal_id_sync(title: str, external_id: str) -> str:
    """
    Generates a stable, unique internal ID for an anime title using its external ID.
    
    The ID combines a 1-3 letter title prefix and a base62-encoded suffix derived 
    from the external_id.
    
    Example: 
    "One Piece" + "iufegefg7-buigbg-374tngg-almsvsv" -> "OP-<base62>"
    "Naruto" + "some-uuid-here" -> "N-<base62>"
    
    Args:
        title: The clean title of the anime (e.g., "Attack on Titan").
        external_id: The external unique identifier (e.g., UUID or other string).
        
    Returns:
        A unique, clean string ID consisting of title prefix, hyphen, and base62 string.
    """
    if not title or not external_id:
        return "DEF-0000"
    
    # Generate prefix from title (1-3 letters) - only alphanumeric
    words = title.split()
    prefix_chars = []
    
    for word in words:
        if word:
            # Get first alphanumeric character from each word
            first_char = next((c for c in word if c.isalnum()), None)
            if first_char:
                prefix_chars.append(first_char.upper())
        
        if len(prefix_chars) >= 3:
            break
    
    prefix = "".join(prefix_chars) if prefix_chars else "DEF"
    
    # Convert external_id to bytes then to integer
    external_bytes = external_id.encode('utf-8')
    external_int = int.from_bytes(external_bytes, byteorder='big')
    
    # Base62 alphabet
    base62_chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    
    # Convert to base62
    if external_int == 0:
        base62_str = base62_chars[0]
    else:
        base62_str = ""
        while external_int > 0:
            base62_str = base62_chars[external_int % 62] + base62_str
            external_int //= 62
    
    return f"{prefix}-{base62_str}"
    
def decode_internal_id_sync(internal_id: str) -> dict:
    MAX_INTERNAL_ID_LEN = 128        # whole string
    MAX_BASE62_LEN = 64              # suffix only
    MAX_DECODED_BYTES = 64           # external_id size cap

    base62_chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

    # ---- basic validation ----
    if not internal_id or '-' not in internal_id:
        return {
            'prefix': None,
            'external_id': None,
            'error': 'Invalid internal_id format'
        }

    if len(internal_id) > MAX_INTERNAL_ID_LEN:
        return {
            'prefix': None,
            'external_id': None,
            'error': 'internal_id too long'
        }

    prefix, base62_str = internal_id.split('-', 1)

    if not prefix or not base62_str:
        return {
            'prefix': None,
            'external_id': None,
            'error': 'Malformed internal_id'
        }

    if len(base62_str) > MAX_BASE62_LEN:
        return {
            'prefix': prefix,
            'external_id': None,
            'error': 'Base62 payload too long'
        }

    # ---- base62 decode ----
    external_int = 0
    for char in base62_str:
        if char not in base62_chars:
            return {
                'prefix': prefix,
                'external_id': None,
                'error': f'Invalid base62 character: {char}'
            }
        external_int = external_int * 62 + base62_chars.index(char)

    # ---- integer → bytes ----
    byte_length = max(1, (external_int.bit_length() + 7) // 8)

    if byte_length > MAX_DECODED_BYTES:
        return {
            'prefix': prefix,
            'external_id': None,
            'error': 'Decoded payload too large'
        }

    external_bytes = external_int.to_bytes(byte_length, byteorder='big')

    # ---- bytes → string ----
    try:
        external_id = external_bytes.decode('utf-8')
    except UnicodeDecodeError:
        return {
            'prefix': prefix,
            'external_id': None,
            'error': 'Decoded data is not valid UTF-8'
        }

    return {
        'prefix': prefix,
        'external_id': external_id
    }


async def generate_internal_id(title: str, external_id: str):
    return await asyncio.to_thread(generate_internal_id_sync, title, external_id)


async def decode_internal_id(internal_id: str):
    return await asyncio.to_thread(decode_internal_id_sync, internal_id)

def encodeURIComponent_sync(s):
    return quote(s, safe="~()*!.'")

async def encodeURIComponent(s):
    return await asyncio.to_thread(encodeURIComponent_sync,s)

def deobfuscate(packed_code):
    # 1. Extract the arguments from the eval(function(...)...) call
    pattern = r'eval\(function\(.*?\)\{(.*?)\}\("(.*?)",(\d+),"(.*?)",(\d+),(\d+),(\d+)\)\)'
    match = re.search(pattern, packed_code, re.S)

    if not match:
        print("No match found")
        return None

    func_body, payload, p1, delimiter, p2, p3, p4 = match.groups()

    p1 = int(p1)
    p2 = int(p2)
    p3 = int(p3)
    p4 = int(p4)

    # 2. Base conversion function (same logic as _0xe46c in JS)
    def base_convert(zq, Pt, lS):
        g = list("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ+/")
        h = g[:Pt]
        i = g[:lS]

        # Convert reversed encoded string → integer
        j = 0
        for power, char in enumerate(reversed(zq)):
            if char in h:
                j += h.index(char) * (Pt ** power)

        # Convert integer to base-lS
        if j == 0:
            return "0"

        result = ""
        while j > 0:
            result = i[j % lS] + result
            j //= lS

        return result

    XP = ""
    Qz = list(delimiter)
    
    i = 0
    length = len(payload)

    # 3. Loop through payload and decode chunks
    while i < length:
        s = ""

        # build chunk until hitting the separator character
        while i < length and payload[i] != Qz[p3]:
            s += payload[i]
            i += 1

        # skip delimiter
        i += 1

        if s:
            # Replace each delimiter symbol with its index
            for idx, symbol in enumerate(Qz):
                s = s.replace(symbol, str(idx))

            # Convert chunk and append the decoded character
            char_code = int(base_convert(s, p3, 10)) - p2
            XP += chr(char_code)

    # 4. Decode unicode
    return urllib.parse.unquote(XP)


def extract_info(js_code):
    # 1. Extract embed URL
    embed_match = re.search(r"var\s+url\s*=\s*['\"]([^'\"]+)['\"]", js_code)
    embed_url = embed_match.group(1) if embed_match else None

    # 2. Extract kwik link from <form action="">
    kwik_match = re.search(r'<form[^>]+action=["\']([^"\']+)', js_code)
    kwik_url = kwik_match.group(1) if kwik_match else None

    # 3. Extract _token value
    token_match = re.search(r'name="_token"\s+value=["\']([^"\']+)', js_code)
    token = token_match.group(1) if token_match else None

    # 4. Extract file size: (109.91 MB)
    size_match = re.search(r'\(([\d\.]+\s*[KMGT]?B)\)', js_code)
    size = size_match.group(1) if size_match else None

    return {
        "embed_url": embed_url,
        "kwik_url": kwik_url,
        "token": token,
        "size": size
    }

