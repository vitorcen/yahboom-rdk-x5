cmake_minimum_required(VERSION 3.8)
project(stereo_combine)

set(CMAKE_CXX_STANDARD 17)
add_compile_options(-O2 -Wall)

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(std_msgs REQUIRED)
find_package(ai_msgs REQUIRED)
find_package(OpenCV REQUIRED COMPONENTS core imgproc imgcodecs)

add_executable(stereo_combine src/stereo_combine.cpp)
ament_target_dependencies(stereo_combine rclcpp sensor_msgs std_msgs ai_msgs)
target_link_libraries(stereo_combine ${OpenCV_LIBS})

install(TARGETS stereo_combine DESTINATION lib/${PROJECT_NAME})
ament_package()
