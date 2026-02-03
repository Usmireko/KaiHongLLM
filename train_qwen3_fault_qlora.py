import os
import json
from datasets import load_dataset
import torch

from transformers import (
    AutoTokenizer,
    AutoModelForCausalLM,
    BitsAndBytesConfig,
)
from trl import SFTTrainer, SFTConfig
from peft import LoraConfig


# ===== 1. 路径配置 =====
MODEL_PATH = "/home/xrh/models/Qwen/Qwen3-8B"           # 确认这个目录存在
DATA_PATH = "/home/xrh/qwen3_os_fault/data/llm_sft_clean.jsonl"
OUTPUT_DIR = "/home/xrh/qwen3_os_fault/qwen3_8b_fault_qlora"

os.environ["WANDB_DISABLED"] = "true"

assert os.path.isdir(MODEL_PATH), f"MODEL_PATH not found or not a directory: {MODEL_PATH}"
assert os.path.isfile(DATA_PATH), f"DATA_PATH not found: {DATA_PATH}"


# ===== 2. Load dataset & split =====
raw = load_dataset("json", data_files=DATA_PATH)

# raw 可能是 DatasetDict({"train": ...}) 或 Dataset（取决于 datasets 版本 / 你的写法）
if isinstance(raw, dict) and "train" in raw:
    ds = raw["train"]
else:
    ds = raw  # 兼容极少数情况下直接返回 Dataset

split = ds.train_test_split(test_size=0.1, seed=42)
train_ds = split["train"]
eval_ds  = split["test"]

# 导出测试集给 infer 用：每行 {"messages":[...]}
TEST_OUT = os.path.join(os.path.dirname(DATA_PATH), "llm_sft_test.jsonl")
with open(TEST_OUT, "w", encoding="utf-8") as f:
    for ex in eval_ds:
        f.write(json.dumps({"messages": ex["messages"]}, ensure_ascii=False) + "\n")
print("[INFO] wrote test set ->", TEST_OUT)

# ===== 3. QLoRA 模型 & tokenizer =====
bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_use_double_quant=True,
    bnb_4bit_compute_dtype=torch.bfloat16,
)

tokenizer = AutoTokenizer.from_pretrained(
    MODEL_PATH,
    use_fast=False,
)
if tokenizer.pad_token is None:
    tokenizer.pad_token = tokenizer.eos_token
tokenizer.model_max_length = 4096
def to_text(example):
    # 训练要包含 assistant 内容，所以 add_generation_prompt=False
    return {
        "text": tokenizer.apply_chat_template(
            example["messages"],
            tokenize=False,
            add_generation_prompt=False,
        )
    }

train_ds = train_ds.map(to_text, remove_columns=train_ds.column_names)
eval_ds  = eval_ds.map(to_text, remove_columns=eval_ds.column_names)


model = AutoModelForCausalLM.from_pretrained(
    MODEL_PATH,
    quantization_config=bnb_config,
    device_map="auto",
)


# ===== 4. LoRA 配置 =====
peft_config = LoraConfig(
    task_type="CAUSAL_LM",
    r=64,
    lora_alpha=16,
    lora_dropout=0.05,
    target_modules=[
        "q_proj",
        "k_proj",
        "v_proj",
        "o_proj",
        "gate_proj",
        "up_proj",
        "down_proj",
    ],
)


# ===== 5. SFTConfig（代替 TrainingArguments）=====
training_args = SFTConfig(
    output_dir=OUTPUT_DIR,
    num_train_epochs=3,
    per_device_train_batch_size=1,
    gradient_accumulation_steps=8,   # 等效 batch≈8
    learning_rate=2e-4,
    lr_scheduler_type="cosine",
    warmup_ratio=0.03,
    logging_steps=10,
    dataset_text_field="text",
    save_steps=500,
    save_total_limit=3,

    bf16=True,                       # 5090 用 bfloat16
    gradient_checkpointing=True,
    optim="paged_adamw_8bit",

    # 你的 trl 版本不支持在 SFTConfig 里写 max_seq_length，
    # 我们先用默认的截断长度（一般是 min(1024, tokenizer.model_max_length)）
    # max_seq_length=4096,

    packing=False,                   # 关掉 packing，避免 attention 实现不兼容
    assistant_only_loss=False,       # 先不做“只训 assistant”以绕开 chat_template 的限制
)


# ===== 6. 构建 SFTTrainer =====
trainer = SFTTrainer(
    model=model,
    args=training_args,
    train_dataset=train_ds,
    eval_dataset=eval_ds,
    peft_config=peft_config,
    processing_class=tokenizer,
)


# ===== 7. 开始训练 =====
trainer.train()

# ===== 8. 保存 LoRA 适配器与 tokenizer =====
trainer.model.save_pretrained(OUTPUT_DIR)
tokenizer.save_pretrained(OUTPUT_DIR)
