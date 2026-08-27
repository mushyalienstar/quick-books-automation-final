import sys
sys.path.insert(0, r'quickbooks-automation-main\app')
from bill_parser import parse_bill

for pdf in [r'Bills\inbox\2026-07 TD_AEROPLAN_VISA_BUSINESS_2508_Jul_06-2026.pdf',
            r'Bills\inbox\2026-07-17 Bell Canada Invoice.pdf',
            r'quickbooks-automation-main\16.99999.10021.273832725.XXXXX2947.000000.pdf']:
    p = parse_bill(pdf)
    print('=' * 70)
    print(pdf)
    print('doc_type:', p['doc_type'], '| date:', p['invoice_date'],
          '| total:', p['total'], '| tax:', p['tax_total'])
    s = p['statement']
    if s:
        txns = s['transactions']
        print(f"txns: {len(txns)}  charges: {s['charges_total']}  credits: {s['credits_total']}")
        print(f"stated purchases: {s['stated_purchases']}  payments&credits: {s['stated_payments_credits']}"
              f"  new bal: {s['stated_new_balance']}  prev bal: {s['stated_previous_balance']}")
        print('reconcile charges:', abs(s['charges_total'] - s['stated_purchases']) < 0.005,
              '| reconcile credits:', abs(-s['credits_total'] - s['stated_payments_credits']) < 0.005)
        for t in txns[:4] + txns[-4:]:
            print(f"  {t['date']}  {t['amount']:>10.2f}  pay={t['is_payment']}  {t['description'][:55]}")
        print('payments flagged:', [f"{t['amount']} {t['description'][:35]}" for t in txns if t['is_payment']])
        print('negatives:', [f"{t['amount']} {t['description'][:35]}" for t in txns if t['amount'] < 0])
    else:
        print('candidates:', len(p['charge_candidates']), '| invoice#:', p['invoice_number'])
