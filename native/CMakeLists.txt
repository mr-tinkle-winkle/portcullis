cmake_minimum_required(VERSION 3.16)
project(portcullis_layershell LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC OFF)

find_package(Qt6 REQUIRED COMPONENTS Core Gui)
find_package(LayerShellQt REQUIRED)

add_library(portcullis_layershell SHARED portcullis_layershell.cpp)
target_link_libraries(portcullis_layershell PRIVATE Qt6::Core Qt6::Gui LayerShellQt::Interface)

install(TARGETS portcullis_layershell LIBRARY DESTINATION lib)
