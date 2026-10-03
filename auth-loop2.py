import asyncio, json, re, time, urllib.request, websockets
from pathlib import Path
from auth_loop_lib import Tab, fill_code, resend_code, EMAIL

async def main():
    tab = Tab(None)
    try:
        await tab.new_tab("https://staging.resumeforu.com/account")
        print("account:", (await tab.body(120)))
        print("signout:", await tab.click_btn("^sign out$|sign out"))
        await asyncio.sleep(6)
        print("url:", (await tab.url())[:90])
        print("body:", (await tab.body(220)))
        b = await tab.body(400)
        signed_out = ("Sign in" in b) and ("Your resumes" not in b[:150])
        print("SIGNOUT RESULT:", "PASS" if signed_out else "FAIL")

        # now the real returning sign-in
        await tab.ev("location.assign('https://staging.resumeforu.com/login?next=/home'); 'nav'")
        await asyncio.sleep(10)
        print("login screen:", (await tab.body(90)))
        print("email:", await tab.set_input("[...document.querySelectorAll('input')].find(i => i.type !== 'checkbox' && i.type !== 'submit')", EMAIL))
        await asyncio.sleep(0.8)
        print("submit:", await tab.click_btn("sign in|continue"))
        await asyncio.sleep(7)
        print("after-email screen:", (await tab.body(90)))
        loop = asyncio.get_event_loop()
        code = await loop.run_in_executor(None, resend_code)
        print("CODE:", code)
        if not code:
            print("SIGNIN RESULT: FAIL (no code)")
            return
        print("code-fill:", await fill_code(tab, code))
        await asyncio.sleep(7)
        print("after-code screen:", (await tab.body(90)))
        if "Continue" in (await tab.body(300)):
            print("continue:", await tab.click_btn("continue"))
            await asyncio.sleep(5)
        final = await tab.wait_url("staging.resumeforu.com", tries=15)
        print("landed:", final[:90])
        print("body:", (await tab.body(200)))
        ok = "/home" in final and "Your resumes" in (await tab.body(400))
        print("SIGNIN RESULT:", "PASS" if ok else "FAIL")
    finally:
        await tab.close()

asyncio.run(main())
