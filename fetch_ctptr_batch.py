import subprocess, sys
regs = ["4001","4011","4018","4021","4022","4023","4026","4027","4031-1","4031","4034","4041","4047","4048","4049","4051","4052","4053","4054","4055","4056","4057","4058","4059","4060","4061","4062","4063-5","4063","4064","4065","4066","4076","4077","4080","4081","4089","4091","4092","4098","4099","4105","4106"]
base = "https://www.cdtfa.ca.gov/lawguides/vol3/ctptr/ctptr-reg{}.html"
failed = []
for r in regs:
    url = base.format(r)
    out = f"ctptr_txt/out_ctptr_{r}.txt"
    try:
        res = subprocess.run([sys.executable, "fetch_full.py", url, out], capture_output=True, text=True, timeout=60)
        print(r, "->", res.stdout.strip() or res.stderr.strip()[:200])
        if res.returncode != 0:
            failed.append(r)
    except Exception as e:
        print(r, "FAILED", e)
        failed.append(r)
print("FAILED:", failed)
