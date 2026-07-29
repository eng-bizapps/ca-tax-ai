import requests, io
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
urls = {
 "ojai_res2143": "https://www.ojai.ca.gov/DocumentCenter/View/1025/21-43-Resolution-Reaffirm-Reset-Cannabis-Business-Tax-Rates-PDF",
 "pacifica_cbp": "https://www.cityofpacifica.org/departments/administrative-services/finance/business-startup/cannabis-business-program",
}
for name,url in urls.items():
    try:
        r = requests.get(url, headers=H, timeout=20)
        print(name, r.status_code, len(r.content), r.headers.get('Content-Type'))
        ct = r.headers.get('Content-Type','')
        if 'pdf' in ct.lower() or url.lower().endswith('pdf'):
            with open(f"tmp_{name}.pdf","wb") as f:
                f.write(r.content)
        else:
            with open(f"tmp_{name}.html","w",encoding="utf-8") as f:
                f.write(r.text)
    except Exception as e:
        print(name, "ERROR", e)
