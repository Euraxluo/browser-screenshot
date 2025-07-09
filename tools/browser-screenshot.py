import os
import base64
import asyncio
from datetime import datetime
from typing import Any, Generator, Callable

import threading
import queue

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

# 假设 browser_use.browser 已经在 PYTHONPATH 下可用
try:
    from browser_use.browser import BrowserSession, BrowserProfile
except Exception as e:
    print("browser_use.browser 未安装或不可用:", e)
    BrowserSession = None
    BrowserProfile = None

class BrowserScreenshotTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        """执行截图，过程中以流式消息反馈进度"""

        url = tool_parameters.get("url")
        width = int(tool_parameters.get("width", 2200))
        height = int(tool_parameters.get("height", 8000))
        device_scale = float(tool_parameters.get("deviceScaleFactor", 2.5))
        cdp_url = tool_parameters.get("cdp_url") or os.getenv("BROWSER_CDP_URL", "http://localhost:9222")

        if not url:
            yield self.create_text_message("Error: url 参数不能为空")
            return

        if BrowserSession is None or BrowserProfile is None:
            yield self.create_text_message("依赖 browser_use.browser 未安装或不可用")
            return

        # 用队列在线程与主线程之间传递进度消息
        progress_q: "queue.Queue[str]" = queue.Queue()
        result_holder: dict[str, Any] = {}

        def progress_reporter(msg: str):
            """供截图协程调用的进度回调"""
            progress_q.put(msg)

        def worker():
            """在独立线程中运行事件循环，避免阻塞 _invoke"""
            try:
                result = asyncio.run(
                    self._take_screenshot(
                        url=url,
                        width=width,
                        height=height,
                        device_scale=device_scale,
                        cdp_url=cdp_url,
                        progress=progress_reporter,
                    )
                )
                result_holder["result"] = result
            except Exception as e:
                result_holder["result"] = {"success": False, "error": str(e)}
            finally:
                # 通知主线程结束
                progress_q.put(None)

        # 启动线程
        t = threading.Thread(target=worker, daemon=True)
        t.start()

        # 主线程消费进度消息并向外部流式输出
        while True:
            msg = progress_q.get()
            if msg is None:
                # 结束信号
                break
            if isinstance(msg, str) and msg:
                yield self.create_text_message(msg)

        # 等待线程结束并获取结果
        t.join()
        result = result_holder.get("result", {"success": False, "error": "未知错误"})

        if result.get("success"):
            screenshot_data = base64.b64decode(result["screenshot_base64"])
            yield self.create_blob_message(
                blob=screenshot_data,
                meta={
                    "mime_type": "image/png",
                    "filename": f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png",
                    "metadata": {
                        "url": url,
                        "page_width": result["page_width"],
                        "page_height": result["page_height"],
                        "deviceScaleFactor": device_scale,
                        "screenshot_path": result["screenshot_path"],
                    },
                },
            )
        else:
            yield self.create_text_message(f"截图失败: {result.get('error')}")

    async def _take_screenshot(
        self,
        url: str,
        width: int,
        height: int,
        device_scale: float,
        cdp_url: str,
        progress: Callable[[str], None] | None = None,
    ) -> dict:
        profile = BrowserProfile(
            headless=True,
            window_size={"width": width, "height": height}
        )
        browser_session = BrowserSession(
            browser_profile=profile,
            cdp_url=cdp_url,
        )
        try:
            if progress:
                progress("🚀 **启动浏览器会话…**\n")
            await browser_session.start()

            if progress:
                progress("🗂️ **创建新标签…**\n")
            await browser_session.create_new_tab()

            if progress:
                progress("🌐 **打开页面并导航…**\n")
            await browser_session.navigate_to(url)

            if progress:
                progress("⏳ **等待页面资源加载…**\n")

            page = await browser_session.get_current_page()
            try:
                # networkidle: 500ms 内无网络请求
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                # 若等待超时则继续，但打印日志供调试
                print("wait_for_load_state timeout, 继续截图 …")

            # 再次获取 page 以防上面步骤失败导致 page 为 None
            page = page or await browser_session.get_current_page()
            if progress:
                progress("📐 **调整视口大小…**\n")

            page_height = await page.evaluate("() => document.documentElement.scrollHeight")
            page_width = await page.evaluate("() => document.documentElement.scrollWidth")
            await page.set_viewport_size({"width": page_width, "height": page_height})
            client = await page.context.new_cdp_session(page)
            await client.send(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": page_width,
                    "height": page_height,
                    "deviceScaleFactor": device_scale,
                    "mobile": False,
                }
            )
            if progress:
                progress("📸 **截图中…**\n")

            await asyncio.sleep(2)
            screenshot_b64 = await browser_session.take_screenshot(full_page=True)

            if progress:
                progress("🖼️ **截图完成，处理文件…**\n")

            screenshots_dir = os.path.join(os.path.dirname(__file__), 'screenshots')
            os.makedirs(screenshots_dir, exist_ok=True)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            screenshot_path = os.path.join(screenshots_dir, f'screenshot_{timestamp}.png')
            with open(screenshot_path, 'wb') as f:
                f.write(base64.b64decode(screenshot_b64))
            if progress:
                progress("✅ **全部任务完成！**\n")

            return {
                "success": True,
                "screenshot_path": screenshot_path,
                "screenshot_base64": screenshot_b64,
                "page_width": page_width,
                "page_height": page_height,
                "url": url,
                "cdp_url": cdp_url,
                "deviceScaleFactor": device_scale
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            try:
                if browser_session.browser_context:
                    current_page = await browser_session.get_current_page()
                    if current_page and not current_page.is_closed():
                        await current_page.close()
            except Exception:
                pass
            await browser_session.close()
