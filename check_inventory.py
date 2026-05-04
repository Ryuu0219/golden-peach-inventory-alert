"""金桃家庫存水位推播檢查"""
import os, sys, json
from datetime import datetime, timedelta, timezone
import gspread
import requests

TW = timezone(timedelta(hours=8))
PUSH_LOG_SHEET = '推播紀錄'  # dedup lock，兩個 system（launchd/gh actions）共用

# ============ 設定 ============
SHEET_ID = "1E4-UyhlZfwxyiZSlPCKlM2GM5RGpb5fmIkTN3mMppzU"
WHITELIST_SHEET_ID = "1cpUoBELuzICPeUjQKyRhyCTRHW0lroq8J6z6fWvZMME"  # 員工 LINE 白名單（單一真相源）
SUBSCRIPTION_CATEGORY = "庫存"  # 此推播屬於「庫存」類別

# 各類別目標庫存天數（= 到貨 + 安全 + 訂貨週期）
TARGET_DAYS = {
    "餡料類": 18,        # 7+4+7
    "皮料類": 14,        # 7+4+3
    "水果配料": 25,      # 7+4+14
    "乳酪類": 18,        # 7+4+7
    "茶葉類": 75,        # 30+15+30
    "肉鬆類": 25,        # 7+4+14
    "紙箱類": 75,        # 30+15+30
    "包裝盒材": 18,      # 7+4+7（已調整）
    "耗材類": 35,        # 14+7+14
    "贈品／袋類": 75,    # 30+15+30
    "贈品/袋類": 75,
    "其他": 35,
}
DEFAULT_TARGET = 30


def get_inventory_subscribers(g):
    """從白名單 Sheet 讀「訂閱類別」含『庫存』或『全部』的主管。"""
    ss = g.open_by_key(WHITELIST_SHEET_ID)
    ws = ss.worksheet('白名單')
    rows = ws.get_all_values()
    if len(rows) < 2:
        return []
    header = rows[0]
    idx_uid = header.index('userId')
    idx_role = header.index('角色類型')
    idx_name = header.index('姓名')
    idx_active = header.index('啟用')
    idx_sub = header.index('訂閱類別')
    out = []
    for r in rows[1:]:
        if len(r) <= idx_sub:
            continue
        if r[idx_active].upper() != 'TRUE':
            continue
        if r[idx_role] == '店員':
            continue
        subs = [s.strip() for s in r[idx_sub].split(',') if s.strip()]
        if SUBSCRIPTION_CATEGORY in subs or '全部' in subs:
            out.append((r[idx_name], r[idx_uid]))
    return out


def gc():
    return gspread.oauth(
        credentials_filename=os.path.expanduser('~/.gspread/credentials.json'),
        authorized_user_filename=os.path.expanduser('~/.gspread/authorized_user.json'),
        scopes=['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    )


def parse_inventory(ss):
    ws = ss.worksheet('📦 完整庫存清單')
    data = ws.get_all_values()
    header_row = None
    for i, r in enumerate(data):
        if '狀態' in r and '品名' in r and '現庫存' in r:
            header_row = i
            break
    if header_row is None:
        raise RuntimeError('找不到 header')
    h = data[header_row]

    def fuzzy(name):
        for i, c in enumerate(h):
            if name in c.replace('\n', '').replace(' ', ''):
                return i
        return None

    idx = {
        '狀態': h.index('狀態'),
        '類別': h.index('類別'),
        '品名': h.index('品名'),
        '現庫存': h.index('現庫存'),
        '單位': h.index('單位'),
        '日均出庫': fuzzy('日均出庫(10日)') or fuzzy('日均出庫'),
        '預估可用天數': fuzzy('預估可用天數'),
    }

    items = []
    for r in data[header_row + 1:]:
        if not any(r):
            continue
        status = r[idx['狀態']].strip() if idx['狀態'] is not None else ''
        cat = r[idx['類別']].strip() if idx['類別'] is not None else ''
        name = r[idx['品名']].strip() if idx['品名'] is not None else ''
        if not name or not cat:
            continue
        try:
            stock = float(r[idx['現庫存']].replace(',', '')) if r[idx['現庫存']] else 0
        except (ValueError, IndexError):
            stock = 0
        unit = r[idx['單位']] if idx['單位'] is not None else ''
        try:
            avg10 = float(r[idx['日均出庫']].replace(',', '')) if idx['日均出庫'] is not None and r[idx['日均出庫']] else 0
        except (ValueError, IndexError):
            avg10 = 0
        days_left_raw = r[idx['預估可用天數']] if idx['預估可用天數'] is not None and len(r) > idx['預估可用天數'] else ''
        try:
            days_left = float(days_left_raw.replace(',', '')) if days_left_raw and days_left_raw != '—' else None
        except ValueError:
            days_left = None
        items.append({
            'status': status, 'cat': cat, 'name': name,
            'stock': stock, 'unit': unit, 'avg10': avg10, 'days_left': days_left
        })
    return items


def compute_30day_avg(ss, name):
    try:
        ws = ss.worksheet('📚 出庫歷史紀錄')
        data = ws.get_all_values()
        if not data:
            return 0
        h = data[0]
        if '日期' not in h or '品名' not in h or '數量' not in h:
            return 0
        di, ni, qi = h.index('日期'), h.index('品名'), h.index('數量')
        cutoff = datetime.now() - timedelta(days=30)
        total = 0
        for r in data[1:]:
            try:
                if r[ni] != name:
                    continue
                d = datetime.strptime(r[di], '%Y/%m/%d')
                if d < cutoff:
                    continue
                total += float(r[qi].replace(',', ''))
            except (ValueError, IndexError):
                continue
        return total / 30
    except Exception:
        return 0


def compute_suggestion(item, ss):
    target_days = TARGET_DAYS.get(item['cat'], DEFAULT_TARGET)
    daily = item['avg10']
    if daily == 0:
        daily = compute_30day_avg(ss, item['name'])
    if daily == 0:
        target_stock = 5
        suggest = max(0, target_stock - item['stock'])
        reason = f"近期無出貨，建議最低備貨 {target_stock} {item['unit']}"
    else:
        target_stock = round(daily * target_days)
        suggest = max(0, target_stock - item['stock'])
        reason = f"目標 {target_days} 天量（{daily:.1f}/天 × {target_days} = {target_stock:g} {item['unit']}）"
    return {
        'suggest': suggest, 'target_stock': target_stock,
        'target_days': target_days, 'daily': daily, 'reason': reason,
    }


def fmt_num(n, suffix=''):
    """整數無小數，浮點保留 1 位"""
    if isinstance(n, float) and n != int(n):
        s = f"{n:.1f}"
    else:
        s = f"{int(n)}"
    return s + suffix


def build_alert_row(item, s, level):
    """單一品項一列：品名 / 剩餘 / 可用天數 / 建議補貨"""
    days = f"{item['days_left']:.1f}天" if item['days_left'] is not None else '—'
    if item['stock'] == 0:
        days = '0天'
    suggest_str = fmt_num(s['suggest']) if s['suggest'] > 0 else '—'
    color = '#a8332f' if level == 'red' else '#c47e1c'
    return {
        "type": "box", "layout": "horizontal", "paddingAll": "6px",
        "contents": [
            {"type": "text", "text": item['name'], "size": "sm", "flex": 5, "wrap": True, "color": "#1a1a1a"},
            {"type": "text", "text": fmt_num(item['stock']) + ' ' + item['unit'],
             "size": "xs", "flex": 3, "align": "end", "color": color},
            {"type": "text", "text": days, "size": "xs", "flex": 2, "align": "end", "color": "#888888"},
            {"type": "text", "text": suggest_str, "size": "xs", "flex": 2, "align": "end",
             "weight": "bold", "color": color},
        ]
    }


def _table_header():
    return {"type": "box", "layout": "horizontal",
            "backgroundColor": "#f5f5f5", "paddingAll": "6px", "cornerRadius": "4px",
            "contents": [
                {"type": "text", "text": "品項", "size": "xs", "color": "#888888", "flex": 5},
                {"type": "text", "text": "剩餘", "size": "xs", "color": "#888888", "flex": 3, "align": "end"},
                {"type": "text", "text": "可用", "size": "xs", "color": "#888888", "flex": 2, "align": "end"},
                {"type": "text", "text": "建議補", "size": "xs", "color": "#888888", "flex": 2, "align": "end"},
            ]}


def build_section(title, color, items_list):
    """整段：標題 + 同類別群組（餡料/皮料/...）+ 表頭 + 多列"""
    contents = [
        {"type": "text", "text": f"{title}（{len(items_list)} 項）",
         "weight": "bold", "size": "md", "color": color},
    ]
    level = 'red' if '警示' in title else 'yellow'

    # 按類別分群（保留原 TARGET_DAYS 順序）
    by_cat = {}
    cat_order = []
    for item, s in items_list:
        cat = item.get('cat') or '未分類'
        if cat not in by_cat:
            by_cat[cat] = []
            cat_order.append(cat)
        by_cat[cat].append((item, s))

    # 單類別 → 不顯示子標題（避免冗餘）；多類別 → 每類加小標
    show_cat_header = len(by_cat) > 1
    for cat in cat_order:
        group = by_cat[cat]
        if show_cat_header:
            contents.append({
                "type": "text", "text": f"  {cat}（{len(group)}）",
                "size": "xs", "color": "#666666", "weight": "bold", "margin": "md",
            })
        contents.append(_table_header())
        for item, s in group:
            contents.append(build_alert_row(item, s, level))

    return {"type": "box", "layout": "vertical", "spacing": "xs", "contents": contents}


def format_flex(items_with_suggestions, now):
    red = [x for x in items_with_suggestions if '🔴' in x[0]['status'] or x[0]['stock'] == 0]
    yellow = [x for x in items_with_suggestions if '🟡' in x[0]['status'] and x not in red]

    body_contents = []
    if red:
        body_contents.append(build_section("🔴 警示", "#a8332f", red))
    if yellow:
        body_contents.append(build_section("🟡 注意", "#c47e1c", yellow))
    if not red and not yellow:
        body_contents.append({
            "type": "text", "text": "✅ 全部品項庫存充足", "size": "md",
            "color": "#2e7d32", "align": "center", "weight": "bold",
        })

    flex = {
        "type": "flex",
        "altText": f"金桃家原料庫存 {now.strftime('%m/%d %H:%M')} - 警示{len(red)} 注意{len(yellow)}",
        "contents": {
            "type": "bubble", "size": "giga",
            "header": {
                "type": "box", "layout": "vertical", "paddingAll": "16px",
                "backgroundColor": "#fdf3f1",
                "contents": [
                    {"type": "text", "text": "🍑 金桃家原料庫存",
                     "weight": "bold", "size": "lg", "color": "#1a1a1a"},
                    {"type": "text", "text": now.strftime('%m/%d %H:%M'),
                     "size": "xs", "color": "#888888", "margin": "sm"},
                ],
            },
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "16px", "spacing": "lg",
                "contents": body_contents,
            },
            "footer": {
                "type": "box", "layout": "vertical", "paddingAll": "12px",
                "contents": [
                    {"type": "text",
                     "text": "剩餘=現庫存 ・可用=預估天數 ・建議補=達目標庫存所需",
                     "size": "xxs", "color": "#aaaaaa", "wrap": True, "align": "center"},
                ],
            },
        },
    }
    return flex


def push_to_line(flex_msg, subscribers):
    env_path = os.path.expanduser('~/.line/golden-peach-oa.env')
    token = None
    with open(env_path) as f:
        for line in f:
            if line.startswith('LINE_CHANNEL_ACCESS_TOKEN='):
                token = line.split('=', 1)[1].strip()
                break
    if not token:
        raise RuntimeError('No LINE token')

    user_ids = [u[1] for u in subscribers]
    resp = requests.post(
        'https://api.line.me/v2/bot/message/multicast',
        json={'to': user_ids, 'messages': [flex_msg]},
        headers={'Authorization': f'Bearer {token}'},
        timeout=10
    )
    return resp.status_code, resp.text


def current_slot():
    """以台灣時間判斷現在屬於 morning（< 12:00）或 afternoon（>= 12:00）。"""
    return 'morning' if datetime.now(TW).hour < 12 else 'afternoon'


def already_pushed(ss, slot):
    """今天該時段是否已推過（跨 launchd/gh actions 共用的鎖）。"""
    today = datetime.now(TW).strftime('%Y-%m-%d')
    try:
        ws = ss.worksheet(PUSH_LOG_SHEET)
    except gspread.WorksheetNotFound:
        return False
    for r in ws.get_all_values()[1:]:
        if len(r) >= 2 and r[0] == today and r[1] == slot:
            return True
    return False


def log_push(ss, slot, source, status):
    """寫一筆推播紀錄。先寫再推，避免 race condition 雙推。"""
    try:
        ws = ss.worksheet(PUSH_LOG_SHEET)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=PUSH_LOG_SHEET, rows=2000, cols=5)
        ws.append_row(['日期', '時段', '來源', '時間戳', '狀態'])
    ws.append_row([
        datetime.now(TW).strftime('%Y-%m-%d'),
        slot,
        source,
        datetime.now(TW).strftime('%Y-%m-%d %H:%M:%S'),
        status,
    ])


def main(dry_run=True):
    g = gc()
    ss = g.open_by_key(SHEET_ID)
    source = os.environ.get('PUSH_SOURCE', 'launchd' if not dry_run else 'manual')
    slot = current_slot()

    if not dry_run and already_pushed(ss, slot):
        print(f"[skip] 今天 {slot} 已由其他來源推過，跳過避免重複")
        return

    items = parse_inventory(ss)
    targets = [i for i in items if '🔴' in i['status'] or '🟡' in i['status']]
    items_with_suggestions = [(i, compute_suggestion(i, ss)) for i in targets]
    flex_msg = format_flex(items_with_suggestions, datetime.now())

    subscribers = get_inventory_subscribers(g)

    red_n = sum(1 for x in items_with_suggestions if '🔴' in x[0]['status'] or x[0]['stock'] == 0)
    yellow_n = len(items_with_suggestions) - red_n

    print("=" * 60)
    print(f"Flex 訊息：警示 {red_n} 項、注意 {yellow_n} 項 / slot={slot} source={source}")
    print("=" * 60)
    print(json.dumps(flex_msg, ensure_ascii=False, indent=2)[:1000] + '...')
    print("=" * 60)
    print(f"推播對象（{len(subscribers)} 人）：{[u[0] for u in subscribers]}")

    if not dry_run:
        if not subscribers:
            print("\n⚠️ 沒有訂閱者，跳過推播")
            return
        # 先寫 log（搶鎖）再推播；萬一 race，後手讀到 log 會 skip
        log_push(ss, slot, source, 'pushing')
        print("\n→ 實際推播中...")
        status, resp = push_to_line(flex_msg, subscribers)
        print(f"LINE API 回應：{status} {resp}")
        log_push(ss, slot, source, f'done {status}')
    else:
        print("\n(DRY RUN — 未實際推播；加 --push 才會推)")


if __name__ == '__main__':
    dry = '--push' not in sys.argv
    main(dry_run=dry)
