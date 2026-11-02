# WEAK_NET 项目配置文件
# 定义项目路径和配置选项

# 项目根目录 (相对于此文件)
PROJECT_ROOT := $(dir $(lastword $(MAKEFILE_LIST)))

# 子项目路径
SERVER_DIR := $(PROJECT_ROOT)server
CLIENT_DIR := $(PROJECT_ROOT)client

# 输出目录
BUILD_DIR := $(SERVER_DIR)/build
BIN_DIR := $(SERVER_DIR)/bin
CLIENT_BIN_DIR := $(CLIENT_DIR)/bin
CLIENT_LIB_DIR := $(CLIENT_DIR)/lib

# 日志目录
LOG_DIR := $(PROJECT_ROOT)logs

# 序列化文件目录
SERIALIZE_DIR := $(PROJECT_ROOT)

# eBPF相关路径
BPF_SOURCE := $(SERVER_DIR)/src/flow_rate.bpf.c
BPF_TARGET := $(BUILD_DIR)/flow_rate.bpf.o

# 导出变量供子Makefile使用
export PROJECT_ROOT
export SERVER_DIR
export CLIENT_DIR
export BUILD_DIR
export BIN_DIR
export CLIENT_BIN_DIR
export CLIENT_LIB_DIR
export LOG_DIR
export SERIALIZE_DIR
export BPF_SOURCE
export BPF_TARGET
