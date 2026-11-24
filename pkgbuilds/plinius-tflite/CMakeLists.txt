# SPDX-License-Identifier: MIT
# libplinius-tflite.so: TensorFlow Lite linked in statically, only the C interface of
# plinius-tflite.h exported. Built with TFLite's own CMake as a subproject.
cmake_minimum_required(VERSION 3.16)
project(plinius-tflite VERSION 0.1 LANGUAGES C CXX)

set(TENSORFLOW_SOURCE_DIR "" CACHE PATH "Unpacked TensorFlow source")
option(PLTFL_WITH_HYBRIS "NPU through the vendor's NNAPI support library (libhybris)" ON)

# Everything goes into a shared library, so the static parts need position-independent code
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_subdirectory("${TENSORFLOW_SOURCE_DIR}/tensorflow/lite"
	"${CMAKE_CURRENT_BINARY_DIR}/tensorflow-lite" EXCLUDE_FROM_ALL)

add_library(plinius-tflite SHARED plinius-tflite.cc)
target_link_libraries(plinius-tflite PRIVATE tensorflow-lite)
set_target_properties(plinius-tflite PROPERTIES
	VERSION ${PROJECT_VERSION}
	SOVERSION 0
	C_VISIBILITY_PRESET hidden
	CXX_VISIBILITY_PRESET hidden
	VISIBILITY_INLINES_HIDDEN ON)
# Symbols of the static archives (TFLite, abseil, XNNPACK ...) stay inside the library
target_link_options(plinius-tflite PRIVATE -Wl,--exclude-libs,ALL -Wl,--no-undefined)
if(PLTFL_WITH_HYBRIS)
	target_compile_definitions(plinius-tflite PRIVATE PLTFL_WITH_HYBRIS)
	target_link_libraries(plinius-tflite PRIVATE hybris-common)
endif()

add_executable(plinius-tflite-bench plinius-tflite-bench.c)
target_link_libraries(plinius-tflite-bench PRIVATE plinius-tflite)

include(GNUInstallDirs)
install(TARGETS plinius-tflite LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR})
install(TARGETS plinius-tflite-bench RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
# makepkg links the sources into the build directory; install the file, not the link
get_filename_component(header plinius-tflite.h REALPATH)
install(FILES "${header}" DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})
configure_file(plinius-tflite.pc.in plinius-tflite.pc @ONLY)
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/plinius-tflite.pc"
	DESTINATION ${CMAKE_INSTALL_LIBDIR}/pkgconfig)
