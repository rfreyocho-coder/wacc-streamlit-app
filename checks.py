"""Stand-alone verification of the math in app.compute(): python checks.py"""
import app

# Hand-checkable example (round numbers)
r = app.compute(beta_u=1.0, tax=0.25, erp=0.06, debt_share=0.30,
                y_us=0.04, y_f=0.09, spread_us=0.01)
de = 0.30 / 0.70
bl = 1.0 * (1 + 0.75 * de)
ke = 0.04 + bl * 0.06
kd = 0.05
w = 0.7 * ke + 0.3 * kd * 0.75
f = 1.09 / 1.04
assert abs(r["beta_l"] - bl) < 1e-12 and abs(r["wacc_usd"] - w) < 1e-12
assert abs(r["ke_f"] - ((1 + ke) * f - 1)) < 1e-12
assert abs(r["spread_f_auto"] - 0.01 * f) < 1e-12
assert abs(r["wacc_f"] - (0.7 * r["ke_f"] + 0.3 * (0.09 + 0.01 * f) * 0.75)) < 1e-12
print(f"example: beta_L={bl:.4f} ke_USD={ke:.4%} WACC_USD={w:.4%} ke_FC={r['ke_f']:.4%} WACC_FC={r['wacc_f']:.4%}")

for name, ok, detail in app.self_checks(r):
    print("PASS" if ok else "FAIL", name, "|", detail)
    assert ok
print("all checks passed")
