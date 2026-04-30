"""金桃家庫存水位推播檢查"""
import os, sys, json
from datetime import datetime, timedelta
import gspread
import requests

# ============ 設定 ============
SHEET_ID = "1E4-UyhlZfwxyiZSlPCKlM2GM5RGpb5fmIkTN3mMppzU"

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

LINE_USER_IDS = [
    ("老闆 Ryuu", "U13190a2b8de52716f397dbf8e7a2dca1"),
    ("老闆娘", "U81f9ddd96c54d811cb00b6d79d183eca"),
]


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


def format_message(items_with_suggestions, now):
    red = [x for x in items_with_suggestions if '🔴' in x[0]['status'] or x[0]['stock'] == 0]
    yellow = [x for x in items_with_suggestions if '🟡' in x[0]['status'] and x not in red]

    lines = [f"🍑 金桃家庫存通知 {now.strftime('%m/%d %H:%M')}", ""]

    if red:
        lines.append(f"🔴 警示（{len(red)} 項）")
        for item, s in red:
            lines.append("")
            lines.append(f"🔴 {item['name']}")
            if item['stock'] == 0:
                lines.append(f"  ⚠️ 已斷貨｜近期出貨 {s['daily']:.1f}/天")
            else:
                days = f"{item['days_left']:.1f} 天" if item['days_left'] is not None else "—"
                lines.append(f"  剩 {item['stock']:g} {item['unit']}｜可用 {days}｜10日均 {s['daily']:.1f}/天")
            if s['suggest'] > 0:
                lines.append(f"  → 建議補 {s['suggest']:g} {item['unit']}（{s['reason']}）")
            else:
                lines.append(f"  → 已超過目標庫存，先不用補")

    if yellow:
        if red:
            lines.append("")
        lines.append(f"🟡 注意（{len(yellow)} 項）")
        for item, s in yellow:
            days = f"{item['days_left']:.1f} 天" if item['days_left'] is not None else "—"
            lines.append("")
            lines.append(f"🟡 {item['name']}")
            lines.append(f"  剩 {item['stock']:g} {item['unit']}｜可用 {days}｜10日均 {s['daily']:.1f}/天")
            if s['suggest'] > 0:
                lines.append(f"  → 建議補 {s['suggest']:g} {item['unit']}")

    if not red and not yellow:
        lines.append("✅ 全部品項庫存充足，無需補貨")

    lines.append("")
    lines.append(f"📊 詳情：https://docs.google.com/spreadsheets/d/{SHEET_ID}")
    return "\n".join(lines)


def push_to_line(message):
    env_path = os.path.expanduser('~/.line/golden-peach-oa.env')
    token = None
    with open(env_path) as f:
        for line in f:
            if line.startswith('LINE_CHANNEL_ACCESS_TOKEN='):
                token = line.split('=', 1)[1].strip()
                break
    if not token:
        raise RuntimeError('No LINE token')

    user_ids = [u[1] for u in LINE_USER_IDS]
    resp = requests.post(
        'https://api.line.me/v2/bot/message/multicast',
        json={'to': user_ids, 'messages': [{'type': 'text', 'text': message}]},
        headers={'Authorization': f'Bearer {token}'},
        timeout=10
    )
    return resp.status_code, resp.text


def main(dry_run=True):
    g = gc()
    ss = g.open_by_key(SHEET_ID)
    items = parse_inventory(ss)
    targets = [i for i in items if '🔴' in i['status'] or '🟡' in i['status']]
    items_with_suggestions = [(i, compute_suggestion(i, ss)) for i in targets]
    msg = format_message(items_with_suggestions, datetime.now())

    print("=" * 60)
    print(f"訊息預覽（{len(msg)} 字）")
    print("=" * 60)
    print(msg)
    print("=" * 60)
    print(f"推播對象：{[u[0] for u in LINE_USER_IDS]}")

    if not dry_run:
        print("\n→ 實際推播中...")
        status, resp = push_to_line(msg)
        print(f"LINE API 回應：{status} {resp}")
    else:
        print("\n(DRY RUN — 未實際推播；加 --push 才會推)")


if __name__ == '__main__':
    dry = '--push' not in sys.argv
    main(dry_run=dry)
