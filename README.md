# IESS - Intelligent Energy Saving System
Run: `pip install -r requirements.txt` then `python app.py` and open http://127.0.0.1:5000 (admin / 1234).
- Detection: YOLOv8-nano (COCO). COCO only knows person, tv, laptop, refrigerator, microwave etc. For LED lights, fans, ACs, projectors
  train/download custom weights and run `set IESS_MODEL=best.pt` (class names matching the Appliance Settings names).
- Without `ultralytics`, the app runs in DEMO mode (simulated boxes, clearly labelled) so the full flow can still be shown.
- Energy numbers are estimates: watts x quantity x editable hours/day x tariff.

## Authentication (signup, OTP, forgot password)
1. `python -m pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill SECRET_KEY and MAIL_* values (Gmail needs an App Password).
   Without MAIL_* values, emails are printed in the terminal instead of being sent (good for testing).
3. `python app.py` then open http://127.0.0.1:5000  (demo login: admin / 1234)
