CC=g++
CXXFLAGS=-std=c++17 -O2 -Wall -Wextra -Wpedantic -pg
INCLUDES=`pkg-config --cflags dbus-1`
LDFLAGS=`pkg-config --libs dbus-1` -lglog -lbpf

SRC=src/server.cpp src/dbus_service.cpp src/looper.cpp src/net_iface.cpp src/serializer.cpp src/net_info.cpp src/weak_netmgr.cpp src/using_iface.cpp src/net_ping.cpp src/rtt_monitor.cpp src/rssi_monitor.cpp src/net_wifiriss.cpp src/net_tcp.cpp src/tcp_loss_monitor.cpp src/event_manager.cpp src/logger.cpp src/traffic_analyzer.cpp src/net_traffic.cpp src/traffic_anomaly_detector.cpp src/network_quality_assessor.cpp src/main.cpp
INC=include/common.hpp include/net_iface.h include/serializer.hpp include/server.hpp include/net_tcp.h include/tcp_loss_monitor.hpp include/weak_netmgr.hpp include/net_info.hpp include/rtt_monitor.hpp include/rssi_monitor.hpp include/dbus_service.hpp include/looper.hpp include/event_manager.hpp include/logger.hpp include/traffic_analyzer.hpp include/network_quality_assessor.hpp

BIN_DIR=bin
BUILD_DIR=build
TARGET=$(BIN_DIR)/weaknet-dbus-server
BPF_TARGET=$(BUILD_DIR)/flow_rate.bpf.o

all: dirs $(BPF_TARGET) $(TARGET)

dirs:
	mkdir -p $(BIN_DIR) $(BUILD_DIR)

$(BPF_TARGET): src/flow_rate.bpf.c
	clang -g -O2 -target bpf -c $< -o $@

$(TARGET): $(SRC) $(INC) $(BPF_TARGET)
	$(CC) $(CXXFLAGS) $(INCLUDES) -Iinclude -o $@ $(SRC) $(LDFLAGS)

.PHONY: clean run

clean:
	rm -rf $(BIN_DIR) $(BUILD_DIR)

run: $(TARGET)
	DBUS_SESSION_BUS_ADDRESS=$$DBUS_SESSION_BUS_ADDRESS $(TARGET)


