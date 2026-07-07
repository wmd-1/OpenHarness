import asyncio
import os
import sys
from datetime import datetime

async def main():
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"generate_video_{timestamp}.log")

    log_file = open(log_path, "a", encoding="utf-8")

    def log(msg):
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line)
        log_file.write(line + "\n")
        log_file.flush()

    log(f"Log file: {log_path}")

    full_prompt = (
        "基于/app/test.pptx这个PPT文件，帮我用hyperframes这个skill，做一个30s的介绍大模型技术原理的宣传视频，"
        "强制要求：①每一帧内容必须视觉丰富，核心元素占据页面比例 70% 以上，避免留白；"
        "②Chrome路径已由环境预配置，直接执行render即可；"
        "③配音使用Qwen-TTS，旁白文本量必须严格适配1.0倍正常语速下的时间长度，30秒总字数严格控制在130-140字之间；"
        "④我已经授权你执行所有代码生成、网页渲染与合成操作，无需向我提问或等待确认。"
    )

    # 丰富后的结构化 Prompt，完美适配 HTML 渲染与 1.0 倍速 TTS
    # full_prompt = (
    #     "【任务目标】\n"
    #     "使用 hyperframes 技能制作一个 30 秒的“大模型技术原理”科普宣传视频。通过生成高视觉冲击力的 HTML/CSS/JS 页面，并将其渲染转录为视频。\n\n"
        
    #     "【核心强制约束】\n"
    #     "1. 视觉饱满度：每一帧的 HTML 页面必须视觉丰富，核心视觉元素（如神经网络图、数据流、魔方等）占据页面比例 70% 以上，严禁出现大面积纯色或白色留白。使用暗色科技感背景（如深蓝、深灰）辅以发光粒子或渐变线条。\n"
    #     "2. 环境配置：Chrome 路径已由环境预配置（PRODUCER_HEADLESS_SHELL_PATH），直接执行 render 即可，切勿生成任何寻找或配置浏览器路径的代码。\n"
    #     "3. 音频与语速：配音使用 Qwen-TTS。旁白文本量必须严格适配 1.0 倍正常语速下的时间长度。30 秒总字数严格控制在 130-140 字之间，禁止堆砌长文本导致配音超时或被迫倍速。\n"
    #     "4. 自动化执行：我已经授权你执行所有代码生成、网页渲染与合成操作，无需向我提问或等待确认。\n\n"
        
    #     "【分段分镜设计（总长 30 秒，精准到秒）】\n\n"
        
    #     "### 第 1 阶段：海量数据输入 (00:00 - 00:06, 共 6 秒)\n"
    #     "- 【旁白 (Qwen-TTS)】：“大模型的诞生，始于海量数据的喂养。千亿级的文本、代码与知识，汇聚成智慧的海洋。” (约 29 字)\n"
    #     "- 【HTML/CSS 视觉实现】：全屏充满密集的“01数据流”、“文本词云”以及旋转的数字地球。利用 CSS Animation 实现数据流从四周向屏幕中央黑洞塌陷汇聚的特效，核心视觉占比 80%，极具压迫感。\n\n"
        
    #     "### 第 2 阶段：Transformer 架构与注意力机制 (00:06 - 00:15, 共 9 秒)\n"
    #     "- 【旁白 (Qwen-TTS)】：“核心秘诀是 Transformer 架构。独特的自注意力机制，让模型能够精准捕捉上下文的千丝万缕。” (约 35 字)\n"
    #     "- 【HTML/CSS 视觉实现】：页面中央呈现一个巨大的、发光的神经网络矩阵。当旁白说到“自注意力”时，通过 JS/CSS 动态连线，让矩阵中的不同节点（代表词汇）之间闪烁爆发高亮的光束，连线和矩阵铺满 75% 的屏幕。\n\n"
        
    #     "### 第 3 阶段：算力训练与参数突变 (00:15 - 00:23, 共 8 秒)\n"
    #     "- 【旁白 (Qwen-TTS)】：“万卡 GPU 昼夜轰鸣，在百亿、千亿参数的疯狂激荡中，语言理解的‘涌现能力’瞬间爆发。” (约 32 字)\n"
    #     "- 【HTML/CSS 视觉实现】：背景切换为密集的服务器机架霓虹灯光效。屏幕中心是一个代表模型的“能量核心”（如 3D 渐变几何体或粒子球），随着时间推移，粒子疯狂旋转、体积变大并最终发生一次全屏“能量扩散”视觉特效，象征能力涌现，视觉占比 80%。\n\n"
        
    #     "### 第 4 阶段：应用落地与未来展望 (00:23 - 00:30, 共 7 秒)\n"
    #     "- 【旁白 (Qwen-TTS)】：“从对话、代码到多模态创作，大模型正成为通往通用人工智能的全新引擎。” (约 28 字)\n"
    #     "- 【HTML/CSS 视觉实现】：左侧展示流畅运行的 AI 对话框、右侧展示自动生成的炫酷代码瀑布，上方是一个不断演进的多模态图像。整体设计紧凑，界面无任何缝隙，最后 2 秒屏幕中央浮现高亮科技感 Slogan：“未来，已然涌现”。\n\n"
        
    #     "请立即根据以上分镜规划，生成对应的 HTML、CSS 动画及 JS 逻辑，并调用 hyperframes 完成渲染。"
    # )
    log(f"Prompt: {full_prompt}")

    proc = await asyncio.create_subprocess_exec(
        "oh", "-p", full_prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            **os.environ,
            "PYTHONUNBUFFERED": "1",
        },
    )
    log(f"Process started, pid={proc.pid}")

    async def read_stream(stream, label):
        while True:
            line = await stream.readline()
            if not line:
                break
            log(f"[{label}] {line.decode('utf-8', errors='replace').rstrip()}")

    await asyncio.gather(
        read_stream(proc.stdout, "stdout"),
        read_stream(proc.stderr, "stderr"),
    )

    returncode = await proc.wait()
    log(f"Process exited with code: {returncode}")
    log_file.close()
    sys.exit(returncode)

if __name__ == "__main__":
    asyncio.run(main())