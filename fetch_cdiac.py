import requests, io, re
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
def get_pdf_text(url):
    r = requests.get(url, headers=H, timeout=30)
    print(url, r.status_code, r.headers.get('Content-Type'), len(r.content))
    if r.status_code==200 and r.content[:4]==b'%PDF':
        import pdfplumber
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    return ""

text = get_pdf_text("https://www.treasurer.ca.gov/cdiac/reports/220105.cannabis.issue.brief.3.pdf")
with open("cdiac_cannabis.txt", "w", encoding="utf-8") as f:
    f.write(text)
print("len", len(text))
for city in ["Hollister","Isleton","Marysville","Maywood","Mammoth"]:
    idxs = [m.start() for m in re.finditer(city, text, re.IGNORECASE)]
    print(city, len(idxs))
