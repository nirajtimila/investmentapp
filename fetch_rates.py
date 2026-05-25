import requests
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

def scan_single_bank(bank):
    brand_name = bank.get("brandName")
    base_uri = bank.get("publicBaseUri")
    
    # Skip providers that do not have a public API configured
    if not base_uri:
        return []
        
    base_uri = base_uri.rstrip('/')
    products_url = f"{base_uri}/cds-au/v1/banking/products"
    
    # We will try different version headers from highest to lowest
    versions_to_try = ["6", "5", "4", "3", "2", "1"]
    products = []
    chosen_version = "4"
    success_list = False
    
    for v in versions_to_try:
        headers = {
            "x-v": v,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        try:
            # Fast timeout (3s) to skip slow/offline banks quickly
            prod_res = requests.get(products_url, headers=headers, timeout=3)
            if prod_res.status_code == 200:
                res_data = prod_res.json()
                products.extend(res_data.get("data", {}).get("products", []))
                chosen_version = v
                success_list = True
                
                # Follow pagination to get all products (up to 5 pages)
                links = res_data.get("links", {})
                next_url = links.get("next")
                page_limit = 4 # 4 more pages max (total 5 pages)
                
                while next_url and page_limit > 0:
                    if not next_url.startswith("http"):
                        next_url = base_uri + next_url
                    
                    next_res = requests.get(next_url, headers=headers, timeout=3)
                    if next_res.status_code == 200:
                        next_data = next_res.json()
                        products.extend(next_data.get("data", {}).get("products", []))
                        next_url = next_data.get("links", {}).get("next")
                        page_limit -= 1
                    else:
                        break
                break
            elif prod_res.status_code in [400, 404, 405, 406]:
                # Server is online but rejected the version/headers, try next version
                continue
            else:
                # Other status code (e.g. 500, 503), break and skip this bank
                break
        except requests.exceptions.RequestException:
            # Connection error or timeout - server is offline, break and skip this bank!
            break
            
    if not success_list or not products:
        return []

    # Filter savings accounts locally in memory
    savings_products = []
    for p in products:
        name_lower = p.get("name", "").lower()
        category = p.get("productCategory")
        
        # Keep only standard retail savings products, excluding business/foreign currency
        is_savings = category == "TRANS_AND_SAVINGS_ACCOUNTS"
        matches_keywords = any(k in name_lower for k in ["saver", "savings", "maximiser", "maximizer", "life", "goal", "progress", "accelerator", "isaver"])
        excludes_keywords = any(k in name_lower for k in ["foreign", "currency", "business", "corporate", "notice", "pension", "super"])
        
        if is_savings and matches_keywords and not excludes_keywords:
            savings_products.append(p)
    
    if not savings_products:
        return []

    bank_rates = []

    # Fetch details for up to 5 savings products per bank
    for product in savings_products[:5]:
        product_id = product.get("productId")
        product_name = product.get("name")
        
        detail_url = f"{base_uri}/cds-au/v1/banking/products/{product_id}"
        
        # Find details with version fallback (chosen_version first, then v4, v3, v1)
        success_detail = False
        deposit_rates = []
        
        # Build optimized list of versions to try
        versions_detail = [chosen_version]
        for fallback in ["6", "5", "4", "3", "2", "1"]:
            if fallback not in versions_detail:
                versions_detail.append(fallback)
                
        for v_detail in versions_detail:
            headers_detail = {
                "x-v": v_detail,
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            try:
                detail_res = requests.get(detail_url, headers=headers_detail, timeout=3)
                if detail_res.status_code == 200:
                    deposit_rates = detail_res.json().get("data", {}).get("depositRates", [])
                    success_detail = True
                    break
                elif detail_res.status_code in [400, 404, 405, 406]:
                    # Version issue, try next version
                    continue
                else:
                    break
            except Exception:
                break
                
        if not success_detail or not deposit_rates:
            continue

        # Parse rates using our additive base + bonus rate logic (excluding introductory rates)
        base_rate = 0.0
        bonus_rate = 0.0
        
        for rate_info in deposit_rates:
            rate_type = rate_info.get("depositRateType")
            rate_val = float(rate_info.get("rate", "0")) * 100
            
            if rate_type == "VARIABLE":
                if rate_val > base_rate:
                    base_rate = rate_val
            elif rate_type == "BONUS":
                # Sum ongoing constant BONUS rates, completely EXCLUDE "INTRODUCTORY" rates
                if rate_val > bonus_rate:
                    bonus_rate = rate_val
        
        if base_rate <= 1.0:
            total_rate = base_rate + bonus_rate
        else:
            total_rate = max(base_rate, bonus_rate)
        
        # Fallback to the maximum non-introductory rate if total_rate is 0.0
        if total_rate == 0.0:
            for rate_info in deposit_rates:
                if rate_info.get("depositRateType") != "INTRODUCTORY":
                    rate_val = float(rate_info.get("rate", "0")) * 100
                    if rate_val > total_rate:
                        total_rate = rate_val
        
        total_rate = round(total_rate, 2)
        
        # Save the product rate if it is valid (excluding introductory and error rates)
        if 0.01 < total_rate < 20.0:
            print(f"Success: {brand_name} - {product_name} ({total_rate}%)")
            bank_rates.append({
                "bank": brand_name,
                "product": product_name,
                "rate_percentage": total_rate
            })
            
    return bank_rates

def fetch_all_bank_rates():
    # 1. Fetch the official list of ALL Data Holders from the ACCC CDR Register
    register_url = "https://api.cdr.gov.au/cdr-register/v1/all/data-holders/brands/summary"
    headers_v1 = {
        "x-v": "1",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    print("Fetching the official list of all Australian banks from the CDR Register...")
    try:
        reg_response = requests.get(register_url, headers=headers_v1, timeout=10)
        reg_response.raise_for_status()
        data_holders = reg_response.json().get("data", [])
    except Exception as e:
        print(f"Failed to load CDR Register: {e}")
        return

    print(f"Found {len(data_holders)} registered data holders. Scanning for ongoing savings rates in parallel...\n")

    all_rates = []
    
    # 2. Run the scraping concurrently using 16 threads
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(scan_single_bank, bank): bank for bank in data_holders}
        for future in as_completed(futures):
            try:
                rates = future.result()
                if rates:
                    all_rates.extend(rates)
            except Exception as e:
                # Keep scraper running even if individual threads fail
                pass

    # 3. Sort alphabetically and save to JSON
    all_rates = sorted(all_rates, key=lambda x: (x['bank'], x['product']))

    with open("current_rates.json", "w", encoding="utf-8") as f:
        json.dump(all_rates, f, indent=4)

    print(f"\nFinished! Successfully grabbed ongoing rates for {len(all_rates)} savings products.")

if __name__ == "__main__":
    fetch_all_bank_rates()
