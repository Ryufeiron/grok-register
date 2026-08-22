import asyncio, json, os

from camoufox import AsyncCamoufox
from playwright_captcha import CaptchaType, ClickSolver, FrameworkType
from playwright_captcha.utils.camoufox_add_init_script.add_init_script import get_addon_path

ADDON_PATH = get_addon_path()


async def main():
    result = {}
    try:
        async with AsyncCamoufox(
            headless=True,
            geoip=True,
            humanize=True,
            i_know_what_im_doing=True,
            config={'forceScopeAccess': True},
            disable_coop=True,
            main_world_eval=True,
            addons=[os.path.abspath(ADDON_PATH)],
        ) as browser:
            context = await browser.new_context()
            page = await context.new_page()
            async with ClickSolver(framework=FrameworkType.CAMOUFOX, page=page) as solver:
                resp = await page.goto(
                    "https://accounts.x.ai/sign-up?redirect=grok-com",
                    wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(5)
                result["status"] = resp.status if resp else None
                result["title"] = await page.title()
                result["frames"] = [f.url[:140] for f in page.frames]
                try:
                    await solver.solve_captcha(
                        captcha_container=page,
                        captcha_type=CaptchaType.CLOUDFLARE_TURNSTILE)
                    result["solver"] = "success"
                except Exception as e:
                    result["solver"] = "fail"
                    result["solver_error"] = str(e)[:600]
                await asyncio.sleep(3)
                token = await page.evaluate(
                    """() => {
                        const el = document.querySelector('input[name="cf-turnstile-response"]');
                        return el ? el.value : null;
                    }""")
                result["token_len"] = len(token) if token else 0
                result["token_head"] = (token or "")[:48]
    except Exception as e:
        result["fatal"] = str(e)[:600]
    print(json.dumps(result, ensure_ascii=False))
    with open("/tmp/turnstile_probe.json", "w") as f:
        json.dump(result, f, ensure_ascii=False)


asyncio.run(main())