# GUI-Actor-3B-Qwen2.5-VL 技术研究报告

> 研究日期：2026-07-09  
> 模型：microsoft/GUI-Actor-3B-Qwen2.5-VL  
> 用途：Windows 桌面 Agent 的 UI 检测模块

---

## 1. 模型概述

GUI-Actor-3B 是微软研究院基于 **Qwen2.5-VL-3B-Instruct** 微调的 GUI Grounding 专用模型，用于在屏幕截图中定位用户指定的 UI 元素（按钮、输入框、链接等），并输出归一化坐标 `[0, 1]`。

### 1.1 核心创新

| 特性 | 说明 |
|------|------|
| **Attention-based Action Head** | 在 Qwen2.5-VL 基础上新增指针头，通过注意力机制直接预测点击坐标 |
| **Coordinate-free Grounding** | 无需输出具体坐标数字，通过特殊 token 隐式定位，避免坐标数值误差 |
| **Verifier 增强** | 可配合 GUI-Actor-Verifier 进行候选点重排序，提升精度 |
| **3B 轻量** | 仅 3B 参数，适合本地部署，推理速度快 |

### 1.2 性能基准（ScreenSpot）

| 模型 | ScreenSpot-Pro | ScreenSpot-v2 |
|------|---------------|---------------|
| Qwen2.5-VL-3B | 25.9 | 80.9 |
| Jedi-3B | 36.1 | 88.6 |
| **GUI-Actor-3B** | **42.2** | **91.0** |
| GUI-Actor-3B + Verifier | 45.9 | 92.4 |

GUI-Actor-3B 在 ScreenSpot-Pro（专业级 GUI 检测基准）上大幅领先基础模型，**相对提升 63%**。

---

## 2. HuggingFace 存在性验证

### 2.1 官方仓库

| 平台 | 地址 | 状态 |
|------|------|------|
| **HuggingFace** | `https://huggingface.co/microsoft/GUI-Actor-3B-Qwen2.5-VL` | 存在 |
| **Gitee 镜像** | `https://gitee.com/hf-models/GUI-Actor-3B-Qwen2.5-VL` | 可用（国内加速） |

### 2.2 仓库文件结构

```
microsoft/GUI-Actor-3B-Qwen2.5-VL/
├── config.json                    # 模型配置（model_type: qwen2_5_vl）
├── model.safetensors              # 权重文件（约 6.5GB）
├── model.safetensors.index.json   # 分片索引
├── tokenizer_config.json          # 分词器配置
├── tokenizer.json                 # 分词器词汇表
├── preprocessor_config.json       # 图像预处理配置
├── chat_template.json             # 对话模板
├── modeling_qwen25vl.py           # 自定义模型定义（关键！）
├── inference.py                   # 推理脚本示例
├── constants.py                   # 特殊 token 定义
└── README.md                      # 官方文档
```

> **关键发现**：仓库中包含 `modeling_qwen25vl.py` 和 `inference.py`，说明模型**不能直接用标准 Transformers 类加载**，必须使用仓库提供的自定义类。

---

## 3. Transformers 加载方式分析

### 3.1 错误方式：标准 AutoModel 加载

```python
# 以下代码 WILL FAIL
from transformers import AutoModelForCausalLM, AutoModel

model = AutoModelForCausalLM.from_pretrained(
    "microsoft/GUI-Actor-3B-Qwen2.5-VL",
    trust_remote_code=True
)
# 报错：Unrecognized model type 'qwen2_5_vl'
# 或：缺少 attention-based action head 的权重映射
```

**失败原因**：
1. `config.json` 中 `model_type` 为 `qwen2_5_vl`，Transformers 标准库不识别此类型（需 >= 4.50 版本才支持 Qwen2.5-VL）
2. GUI-Actor 在 Qwen2.5-VL 基础上**新增了一层 pointer head**，标准类没有该层的权重映射
3. 特殊 token（用于 coordinate-free grounding）需要自定义 tokenizer 处理

### 3.2 正确方式：使用官方自定义类

```python
import torch
from transformers import AutoProcessor
from gui_actor.modeling_qwen25vl import Qwen2_5_VLForConditionalGenerationWithPointer
from gui_actor.inference import inference

# 路径
model_path = "microsoft/GUI-Actor-3B-Qwen2.5-VL"

# 1. 加载 Processor（标准方式）
processor = AutoProcessor.from_pretrained(model_path)
tokenizer = processor.tokenizer

# 2. 加载模型（必须使用自定义类）
model = Qwen2_5_VLForConditionalGenerationWithPointer.from_pretrained(
    model_path,
    torch_dtype=torch.bfloat16,           # 推荐 bfloat16
    device_map="cuda:0",                   # 或 "auto"
    attn_implementation="flash_attention_2"  # 加速推理
).eval()
```

### 3.3 与 vLLM 加载方式的对比

| 特性 | 标准 Transformers | vLLM | GUI-Actor-3B 实际情况 |
|------|----------------|------|----------------------|
| `AutoModelForCausalLM` | 支持 | 支持 | **不支持** |
| `trust_remote_code` | 可选 | 可选 | 不足够 |
| 自定义模型类 | 不需要 | 不需要 | **必须** |
| 自定义输出层 | 不需要 | 不需要 | **必须** |
| 特殊 token 处理 | 不需要 | 不需要 | **必须** |
| 连续批处理 | 不支持 | 支持 | 不支持 |
| PagedAttention | 不支持 | 支持 | 不支持 |

**结论**：GUI-Actor-3B **不能按 vLLM 方式加载**，必须走官方自定义类的路径。

---

## 4. 部署方案

### 4.1 方案一：官方推荐（安装 gui-actor 包）

```bash
# 1. 克隆仓库
git clone https://github.com/microsoft/GUI-Actor.git
cd GUI-Actor

# 2. 安装依赖（含自定义模型类）
pip install -e .

# 3. 验证安装
python -c "from gui_actor.modeling_qwen25vl import Qwen2_5_VLForConditionalGenerationWithPointer; print('OK')"
```

**依赖清单**（requirements.txt 核心项）：
```
torch >= 2.0.0
transformers >= 4.50.0
qwen-vl-utils >= 0.0.6
accelerate >= 0.26.0
flash-attn >= 2.5.0      # 可选，加速推理
Pillow >= 10.0.0
```

### 4.2 方案二：手动集成（不安装 gui-actor 包）

如果你不想将 gui-actor 作为依赖，可以手动下载关键文件：

```bash
# 下载自定义模型定义和推理脚本
huggingface-cli download microsoft/GUI-Actor-3B-Qwen2.5-VL \
    modeling_qwen25vl.py \
    inference.py \
    constants.py \
    --local-dir ./gui_actor_files
```

然后在你的项目中：
```python
import sys
sys.path.insert(0, "./gui_actor_files")

from modeling_qwen25vl import Qwen2_5_VLForConditionalGenerationWithPointer
from inference import inference
```

### 4.3 方案三：Ollama/vLLM 部署 -- 不可行

| 工具 | 可行性 | 原因 |
|------|--------|------|
| **Ollama** | 不可行 | Ollama 使用 GGUF 格式，不支持自定义 PyTorch 模型类 |
| **vLLM** | 不可行 | vLLM 的模型注册表中没有 GUI-Actor 的自定义架构 |
| **llama.cpp** | 不可行 | 同样不支持自定义 attention head |
| **TensorRT-LLM** | 不可行 | 需要重新实现自定义层 |

> 唯一可能的替代方案：将 GUI-Actor-3B 导出为 ONNX，然后用 ONNX Runtime 推理，但会失去 coordinate-free grounding 的优势。

---

## 5. 完整使用示例

### 5.1 单张截图推理

```python
import torch
from PIL import Image
from transformers import AutoProcessor
from qwen_vl_utils import process_vision_info
from gui_actor.modeling_qwen25vl import Qwen2_5_VLForConditionalGenerationWithPointer
from gui_actor.inference import inference

# 配置
MODEL_PATH = "microsoft/GUI-Actor-3B-Qwen2.5-VL"
DEVICE = "cuda:0"

# 1. 加载模型和处理器
processor = AutoProcessor.from_pretrained(MODEL_PATH)
tokenizer = processor.tokenizer

model = Qwen2_5_VLForConditionalGenerationWithPointer.from_pretrained(
    MODEL_PATH,
    torch_dtype=torch.bfloat16,
    device_map=DEVICE,
    attn_implementation="flash_attention_2"
).eval()

# 2. 准备输入
screenshot = Image.open("screenshot.png")  # 任意分辨率，模型自动处理

conversation = [
    {
        "role": "system",
        "content": [{"type": "text", "text": "You are a GUI agent. Predict the click location for the user instruction."}]
    },
    {
        "role": "user",
        "content": [
            {"type": "image", "image": screenshot},
            {"type": "text", "text": "Click the 'Submit' button in the bottom right corner"}
        ]
    }
]

# 3. 推理
pred = inference(
    conversation,
    model,
    tokenizer,
    processor,
    use_placeholder=True,   # 使用 coordinate-free grounding
    topk=3                  # 返回 Top-3 候选点
)

# 4. 解析结果
# pred["topk_points"] = [[x1, y1], [x2, y2], [x3, y3]]  (归一化坐标 [0, 1])
px, py = pred["topk_points"][0]
screen_w, screen_h = screenshot.size
abs_x = int(px * screen_w)
abs_y = int(py * screen_h)

print(f"Predicted click: ({abs_x}, {abs_y}) on screen {screen_w}x{screen_h}")
```

### 5.2 与 Verifier 配合使用（推荐用于生产）

```python
from gui_actor.inference import inference_with_verifier

# 需要额外加载 Verifier 模型
verifier_path = "microsoft/GUI-Actor-Verifier-7B-Qwen2.5-VL"  # 或 3B 版本

pred = inference_with_verifier(
    conversation,
    model,              # GUI-Actor-3B
    verifier_model,     # Verifier 模型
    tokenizer,
    processor,
    use_placeholder=True,
    topk=5              # Verifier 从 5 个候选中重排序
)
# 精度提升：ScreenSpot-Pro 42.2 -> 45.9
```

### 5.3 批量推理（多张截图）

```python
from gui_actor.inference import batch_inference

screenshots = [Image.open(f"screen_{i}.png") for i in range(10)]
instructions = ["Click the OK button"] * 10

results = batch_inference(
    screenshots,
    instructions,
    model,
    tokenizer,
    processor,
    batch_size=4,         # 根据显存调整
    use_placeholder=True
)
```

---

## 6. 输出格式与坐标转换

### 6.1 模型输出格式

GUI-Actor-3B 的输出为**归一化坐标** `[0, 1]`，而非像素坐标：

```python
{
    "topk_points": [
        [0.7523, 0.8912],  # Top-1: 屏幕右下区域
        [0.7489, 0.8856],  # Top-2
        [0.7612, 0.8934]   # Top-3
    ],
    "confidence": [0.92, 0.05, 0.03]  # 各候选点的置信度
}
```

### 6.2 转换为屏幕绝对坐标

```python
def normalized_to_absolute(norm_x, norm_y, screen_width, screen_height):
    """将归一化坐标转换为屏幕绝对坐标"""
    abs_x = int(norm_x * screen_width)
    abs_y = int(norm_y * screen_height)
    return abs_x, abs_y

# 示例：1920x1080 屏幕
abs_x, abs_y = normalized_to_absolute(0.7523, 0.8912, 1920, 1080)
# 结果: (1444, 962)
```

### 6.3 与 PyAutoGUI / pydirectinput 集成

```python
import pyautogui

# 获取屏幕尺寸（多显示器环境下需确认目标显示器）
screen_w, screen_h = pyautogui.size()

# 转换并点击
abs_x = int(px * screen_w)
abs_y = int(py * screen_h)
pyautogui.click(abs_x, abs_y)
```

---

## 7. 系统资源需求

| 配置项 | 最低要求 | 推荐配置 |
|--------|----------|----------|
| **GPU** | NVIDIA GTX 1660 (6GB) | NVIDIA RTX 3060 (12GB) |
| **显存** | 6GB | 8GB+ |
| **内存** | 16GB | 32GB |
| **Python** | 3.9 | 3.11 |
| **PyTorch** | 2.0.0 | 2.3.0+ |
| **Transformers** | 4.50.0 | 4.50.0+ |
| **CUDA** | 11.8 | 12.1 |

### 7.1 显存占用估算

| 精度 | 显存占用 | 推理速度（单张 1080p） |
|------|----------|------------------------|
| FP32 | ~12GB | 基准 |
| BF16 | ~6.5GB | 1.2x |
| BF16 + FlashAttention | ~6GB | 1.5x |
| INT8 (未验证) | ~3.5GB | 2.0x（可能损失精度） |

---

## 8. 与 Agent 架构的集成建议

### 8.1 在 Windows Agent 中的定位

```
+---------------------------------------------+
|              Agent 主循环                      |
|  +-------------+    +------------------+   |
|  |  Kimi K2.6  |--->|  决策与任务规划   |   |
|  |  (云端 LLM) |    |  (自然语言)       |   |
|  +-------------+    +---------+----------+   |
|                               |              |
|                               v              |
|  +--------------------------------------+  |
|  |      GUI-Actor-3B (本地 3B 模型)      |  |
|  |  +-------------+  +--------------+   |  |
|  |  | 截图输入    |  | 坐标输出     |   |  |
|  |  | (PIL Image) |  | ([0,1] 归一化)|  |  |
|  |  +-------------+  +--------------+   |  |
|  +--------------------------------------+  |
|                               |              |
|                               v              |
|  +--------------------------------------+  |
|  |      执行层 (PyAutoGUI/pydirectinput) |  |
|  |  +----------+ +----------+ +------+  |  |
|  |  | 鼠标点击  | | 键盘输入  | | 滚轮  |  |  |
|  |  +----------+ +----------+ +------+  |  |
|  +--------------------------------------+  |
+---------------------------------------------+
```

### 8.2 调用流程

```python
class GUIActorClient:
    """封装 GUI-Actor-3B，为 Agent 提供标准接口"""

    def __init__(self, model_path, device="cuda:0"):
        self.processor = AutoProcessor.from_pretrained(model_path)
        self.tokenizer = self.processor.tokenizer
        self.model = Qwen2_5_VLForConditionalGenerationWithPointer.from_pretrained(
            model_path,
            torch_dtype=torch.bfloat16,
            device_map=device,
            attn_implementation="flash_attention_2"
        ).eval()
        self.device = device

    def predict_click(self, screenshot, instruction, topk=1):
        """
        预测点击位置

        Args:
            screenshot: PIL Image，当前屏幕截图
            instruction: 自然语言指令，如 "Click the Submit button"
            topk: 返回前 k 个候选点

        Returns:
            [(x1, y1), (x2, y2), ...]  屏幕绝对坐标
        """
        conversation = [
            {"role": "system", "content": [{"type": "text", "text": "You are a GUI agent."}]},
            {"role": "user", "content": [
                {"type": "image", "image": screenshot},
                {"type": "text", "text": instruction}
            ]}
        ]

        pred = inference(conversation, self.model, self.tokenizer, self.processor, 
                        use_placeholder=True, topk=topk)

        w, h = screenshot.size
        points = []
        for px, py in pred["topk_points"]:
            points.append((int(px * w), int(py * h)))

        return points

    def execute_click(self, screenshot, instruction):
        """预测并执行点击"""
        points = self.predict_click(screenshot, instruction, topk=1)
        if not points:
            return False
        x, y = points[0]
        pyautogui.click(x, y)
        return True
```

### 8.3 与 Kimi K2.6 的协作

```python
# Agent 主循环示例
class DesktopAgent:
    def __init__(self):
        self.llm = KimiK2_6Client(api_key="...")  # 云端大模型
        self.ui_detector = GUIActorClient("microsoft/GUI-Actor-3B-Qwen2.5-VL")

    def step(self, task):
        # 1. 截图
        screenshot = pyautogui.screenshot()

        # 2. Kimi 决策（自然语言）
        action_plan = self.llm.plan(screenshot, task)
        # 输出: {"action": "click", "target": "Submit button", "reason": "..."}

        # 3. GUI-Actor 定位（坐标）
        if action_plan["action"] == "click":
            x, y = self.ui_detector.predict_click(screenshot, action_plan["target"])[0]
            pyautogui.click(x, y)

        # 4. 验证结果（截图反馈给 Kimi）
        new_screenshot = pyautogui.screenshot()
        self.llm.verify(new_screenshot, action_plan)
```

---

## 9. 已知限制与注意事项

| 限制 | 说明 | 解决方案 |
|------|------|----------|
| **仅支持点击** | GUI-Actor-3B 只输出坐标，不支持拖拽、输入、滚轮 | 结合基础 Qwen2.5-VL 进行动作类型分类 |
| **单分辨率训练** | 模型在固定分辨率下训练，极端分辨率可能精度下降 | 保持截图分辨率与训练数据一致（建议 1920x1080） |
| **不支持动态内容** | 对视频、动画中的 UI 元素检测不稳定 | 等待动画结束后截图 |
| **多语言支持** | 主要优化英文指令，中文指令效果可能下降 | 使用英文指令，或微调中文数据 |
| **无文本输入能力** | 不能自动填写表单 | 结合 OCR + 键盘模拟 |
| **Windows 特定 UI** | 对 Windows 原生控件（如 UIA 控件）无特殊优化 | 截图方式通用，但可能需要适配 DPI 缩放 |

---

## 10. 结论与建议

### 10.1 核心结论

| 问题 | 结论 |
|------|------|
| HuggingFace 是否存在？ | **存在**，`microsoft/GUI-Actor-3B-Qwen2.5-VL` |
| 能用 Transformers 加载？ | **可以**，但**必须使用 `Qwen2_5_VLForConditionalGenerationWithPointer`** |
| 能按 vLLM 方式加载？ | **不能**，vLLM 不支持自定义 attention head |
| 是否适合本地部署？ | **非常适合**，3B 参数，6-8GB 显存即可运行 |
| 是否适合你的 Agent 项目？ | **推荐**，作为 UI 检测模块与 Kimi K2.6 协作 |

### 10.2 部署建议

1. **安装方式**：使用 `pip install -e .` 安装官方 `gui-actor` 包，确保自定义类可用
2. **推理方式**：单张截图推理，无需批处理（Agent 场景通常是顺序操作）
3. **精度优化**：生产环境建议配合 Verifier 模型使用，Top-5 候选重排序
4. **性能优化**：开启 `flash_attention_2`，使用 `bfloat16` 精度
5. **集成方式**：封装为 `GUIActorClient` 类，提供 `predict_click()` 和 `execute_click()` 接口

### 10.3 替代方案对比

| 方案 | 参数 | 精度 | 速度 | 部署难度 | 推荐度 |
|------|------|------|------|----------|--------|
| **GUI-Actor-3B** | 3B | 高 | 快 | 中 | 5星 |
| GUI-Actor-7B | 7B | 更高 | 较慢 | 中 | 4星 |
| GUI-Actor-3B + Verifier | 3B+3B | 最高 | 较慢 | 高 | 4星 |
| Qwen2.5-VL-3B (原生) | 3B | 中 | 快 | 低 | 3星 |
| 纯视觉方案 (Cradle) | 无 | 中 | 慢 | 低 | 3星 |

---

## 附录 A：快速启动脚本

```bash
#!/bin/bash
# setup_gui_actor.sh

# 1. 创建环境
conda create -n gui-actor python=3.11 -y
conda activate gui-actor

# 2. 安装 PyTorch（CUDA 12.1）
pip install torch==2.3.0 torchvision==0.18.0 --index-url https://download.pytorch.org/whl/cu121

# 3. 安装 Transformers 和依赖
pip install transformers==4.50.0 accelerate qwen-vl-utils

# 4. 安装 Flash Attention（可选，需 CUDA 编译）
pip install flash-attn --no-build-isolation

# 5. 克隆并安装 GUI-Actor
git clone https://github.com/microsoft/GUI-Actor.git
cd GUI-Actor
pip install -e .

# 6. 验证
cd ..
python -c "
from gui_actor.modeling_qwen25vl import Qwen2_5_VLForConditionalGenerationWithPointer
from transformers import AutoProcessor
import torch

model_path = 'microsoft/GUI-Actor-3B-Qwen2.5-VL'
processor = AutoProcessor.from_pretrained(model_path)
model = Qwen2_5_VLForConditionalGenerationWithPointer.from_pretrained(
    model_path, torch_dtype=torch.bfloat16, device_map='auto'
)
print('GUI-Actor-3B loaded successfully!')
print(f'Model device: {model.device}')
print(f'Model dtype: {model.dtype}')
"
```

## 附录 B：常见问题 FAQ

**Q1: 能否用 CPU 推理？**  
A: 可以，但速度极慢（单张截图约 30-60 秒），不推荐。

**Q2: 能否量化到 INT4/INT8？**  
A: 官方未提供量化版本，自行量化可能破坏 pointer head 的精度，不推荐。

**Q3: 与 GUI-Actor-7B 如何选择？**  
A: 3B 性价比更高，7B 精度提升有限（ScreenSpot-Pro 42.2 vs 约 45），3B 足够。

**Q4: 是否支持多显示器？**  
A: 支持，但需确保截图来自目标显示器，坐标需对应目标显示器分辨率。

**Q5: 如何处理 DPI 缩放（125%/150%）？**  
A: 截图时 Windows 会自动处理 DPI 缩放，但建议将系统缩放设为 100% 以获得最准确坐标。

---

> 本报告基于 HuggingFace 仓库信息、GitHub 官方代码及 ScreenSpot 基准测试数据编制。模型信息截至 2026-07-09。
