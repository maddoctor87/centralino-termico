PLC21 MicroPython package for Industrial Shields ESP32 PLC 21+

Files:
- plc21_map.py
- plc21_io.py
- devices/mcp23008.py
- devices/ads1015.py
- devices/pca9685.py

Example:
    from plc21_io import PLC21IO
    io = PLC21IO()
    print(io.model_name())
    print(io.scan())
    print(io.read_input("I0.0"))
    print(io.read_input_raw("I0.11"))
    io.output_on("Q0.1")
    io.output_off("Q0.1")
    io.write_output("A0.5", 2048)

Notes:
- Package tailored to model 21+
- Based on reverse engineering of Industrial Shields Arduino core
- Physical output validation may still require tests on the electrical panel
