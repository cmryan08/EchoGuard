# EchoGuard CSI receiver.
# Derived from espressif/esp-csi examples/get-started/csi_recv.
# Changes vs. official: fixed project name and no git_describe() call so the
# project builds outside of an esp-csi git checkout.
cmake_minimum_required(VERSION 3.5)
add_compile_options(-fdiagnostics-color=always)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

project(echoguard_rx)
