cmake_minimum_required(VERSION 3.8)
project(safety_stop)

if(NOT CMAKE_CXX_STANDARD)
  set(CMAKE_CXX_STANDARD 17)
endif()
add_compile_options(-Wall -Wextra)

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(std_msgs REQUIRED)

add_executable(safety_stop src/safety_stop.cpp)
ament_target_dependencies(safety_stop rclcpp geometry_msgs sensor_msgs std_msgs)

install(TARGETS safety_stop DESTINATION lib/${PROJECT_NAME})

ament_package()
