# 包含配置文件
include config.mk

CC=g++
CXXFLAGS=-std=c++17 -O2 -Wall -Wextra -Wpedantic -pg
LDFLAGS=`pkg-config --libs dbus-1` -lglog
INCLUDES=`pkg-config --cflags dbus-1`

SRC_SERVER=server.cpp serializer.cpp
SRC_CLIENT_LIB=client/client.cpp server/src/serializer.cpp
SRC_CLIENT_TEST=client/test_client.cpp

all: dirs server-client-lib

server-client-lib:
	@$(MAKE) -C server
	@echo "编译WeakNet客户端动态库..."
	@mkdir -p $(CLIENT_LIB_DIR) $(CLIENT_BIN_DIR)
	$(CC) $(CXXFLAGS) $(INCLUDES) -I$(SERVER_DIR)/include -I$(CLIENT_DIR) -fPIC -shared -o $(CLIENT_LIB_DIR)/libweaknet.so $(SRC_CLIENT_LIB) $(LDFLAGS)
	@echo "编译WeakNet客户端测试程序..."
	$(CC) $(CXXFLAGS) $(INCLUDES) -I$(SERVER_DIR)/include -I$(CLIENT_DIR) -o $(CLIENT_BIN_DIR)/test-client $(SRC_CLIENT_TEST) -L$(CLIENT_LIB_DIR) -lweaknet $(LDFLAGS)

# 支持原来的命名，保持兼容性
server-client: server-client-lib

dirs:
	mkdir -p $(BUILD_DIR) $(BIN_DIR) $(CLIENT_BIN_DIR) $(CLIENT_LIB_DIR)

.PHONY: clean run-server run-client

clean:
	rm -rf $(BUILD_DIR) $(BIN_DIR) $(CLIENT_BIN_DIR) $(CLIENT_LIB_DIR)
	rm -f *.bin

run-server: $(BIN_DIR)/weaknet-dbus-server
	DBUS_SESSION_BUS_ADDRESS=$$DBUS_SESSION_BUS_ADDRESS $(BIN_DIR)/weaknet-dbus-server

test-client: server-client-lib
	@if [ "$(COMMAND)" = "" ]; then \
		echo "用法: make test-client COMMAND=[all|get|health|file|ping|check|events|event-types|test-*]"; \
		echo "示例: make test-client COMMAND=all"; \
		echo "      make test-client COMMAND=get"; \
		echo "      make test-client COMMAND=ping google.com"; \
		echo "      make test-client COMMAND=test-basic"; \
	else \
		echo "运行客户端测试程序: $(COMMAND)"; \
		LD_LIBRARY_PATH=$(CLIENT_LIB_DIR):$$LD_LIBRARY_PATH DBUS_SESSION_BUS_ADDRESS=$$DBUS_SESSION_BUS_ADDRESS $(CLIENT_BIN_DIR)/test-client $(COMMAND); \
	fi

test-lib: server-client-lib
	@echo "运行动态库基本功能测试..."
	LD_LIBRARY_PATH=$(CLIENT_LIB_DIR):$$LD_LIBRARY_PATH DBUS_SESSION_BUS_ADDRESS=$$DBUS_SESSION_BUS_ADDRESS $(CLIENT_BIN_DIR)/test-client lib-test

test-events: server-client-lib
	@echo "运行事件监听功能测试..."
	LD_LIBRARY_PATH=$(CLIENT_LIB_DIR):$$LD_LIBRARY_PATH DBUS_SESSION_BUS_ADDRESS=$$DBUS_SESSION_BUS_ADDRESS $(CLIENT_BIN_DIR)/test-client test-events

test-all: server-client-lib
	@echo "运行完整接口验证测试..."
	LD_LIBRARY_PATH=$(CLIENT_LIB_DIR):$$LD_LIBRARY_PATH DBUS_SESSION_BUS_ADDRESS=$$DBUS_SESSION_BUS_ADDRESS $(CLIENT_BIN_DIR)/test-client all

test-ping: server-client-lib
	@echo "运行Ping功能测试..."
	LD_LIBRARY_PATH=$(CLIENT_LIB_DIR):$$LD_LIBRARY_PATH DBUS_SESSION_BUS_ADDRESS=$$DBUS_SESSION_BUS_ADDRESS $(CLIENT_BIN_DIR)/test-client test-ping

test-performance: server-client-lib
	@echo "运行性能测试..."
	LD_LIBRARY_PATH=$(CLIENT_LIB_DIR):$$LD_LIBRARY_PATH DBUS_SESSION_BUS_ADDRESS=$$DBUS_SESSION_BUS_ADDRESS $(CLIENT_BIN_DIR)/test-client test-performance

run-client: server-client-lib
	@echo "运行客户端订阅模式..."
	LD_LIBRARY_PATH=$(CLIENT_LIB_DIR):$$LD_LIBRARY_PATH DBUS_SESSION_BUS_ADDRESS=$$DBUS_SESSION_BUS_ADDRESS $(CLIENT_BIN_DIR)/test-client subscribe



