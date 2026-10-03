import asyncio, json
from auth_loop_lib import Tab, fill_code, resend_code, EMAIL

async def main():
    tab = Tab(None)
    try:
        # signed-out state (fresh cookie clear); go to /account -> bounced to Logto sign-in
        await tab.new_tab("https://staging.resumeforu.com/account")
        print("start screen:", (await tab.body(90)))
        print("email:", await tab.set_input("[...document.querySelectorAll('input')].find(i => i.type !== 'checkbox' && i.type !== 'submit')", EMAIL))
        await asyncio.sleep(1)
        print("submit:", await tab.click_btn("^sign in$"))
        await asyncio.sleep(7)
        print("after-submit:", (await tab.body(90)))
        code = await asyncio.get_event_loop().run_in_executor(None, resend_code)
        print("CODE:", code)
        if not code:
            print("SIGNIN RESULT: FAIL(no-code)"); return
        print("code-fill:", await fill_code(tab, code))
        await asyncio.sleep(7)
        b = await tab.body(300)
        print("after-code:", b)
        if "Continue" in b:
            print("continue:", await tab.click_btn("continue"))
            await asyncio.sleep(5)
        final = await tab.wait_url("staging.resumeforu.com", tries=18)
        print("landed:", final[:90])
        body = await tab.body(300)
        print("body:", body)
        ok = "/home" in final and "Your resumes" in body and "Sign in to your account" not in body
        print("SIGNIN RESULT:", "PASS" if ok else "FAIL")
    finally:
        await tab.close()

asyncio.run(main())
