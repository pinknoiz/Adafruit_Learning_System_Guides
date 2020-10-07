import time
import board
import busio
from digitalio import DigitalInOut, Direction, Pull
import adafruit_dotstar as dotstar
from adafruit_esp32spi import adafruit_esp32spi, adafruit_esp32spi_wifimanager
from adafruit_io.adafruit_io import IO_HTTP, AdafruitIO_RequestError
from simpleio import map_range

import adafruit_pm25
import adafruit_bme280


### WiFi ###

# Get wifi details and more from a secrets.py file
try:
    from secrets import secrets
except ImportError:
    print("WiFi secrets are kept in secrets.py, please add them there!")
    raise

# If you have an externally connected ESP32:
esp32_cs = DigitalInOut(board.D13)
esp32_reset = DigitalInOut(board.D12)
esp32_ready = DigitalInOut(board.D11)

spi = busio.SPI(board.SCK, board.MOSI, board.MISO)
esp = adafruit_esp32spi.ESP_SPIcontrol(spi, esp32_cs, esp32_ready, esp32_reset)
status_light = dotstar.DotStar(board.APA102_SCK, board.APA102_MOSI, 1, brightness=0.2)
wifi = adafruit_esp32spi_wifimanager.ESPSPI_WiFiManager(esp, secrets, status_light)

# Connect to a PM2.5 sensor over UART
uart = busio.UART(board.TX, board.RX, baudrate=9600)
pm25 = adafruit_pm25.PM25_UART(uart)

# Connect to a BME280 sensor over I2C
i2c = busio.I2C(board.SCL, board.SDA)
bme280 = adafruit_bme280.Adafruit_BME280_I2C(i2c)

### Sensor Functions ###
def calculate_aqi(pm_sensor_reading):
    """Returns a calculated air quality index (AQI)
    and category as a tuple.
    NOTE: The AQI returned by this function should ideally be measured
    using the 24-hour concentration average. Calculating a AQI without
    averaging will result in higher AQI values than expected.
    :param float pm_sensor_reading: Particulate matter sensor value.

    """
    # Check sensor reading using EPA breakpoint (Clow-Chigh)
    if 0.0 <= pm_sensor_reading <= 12.0:
        # AQI calculation using EPA breakpoints (Ilow-IHigh)
        aqi = map_range(int(pm_sensor_reading), 0, 12, 0, 50)
        aqi_category = "Good"
    elif 12.1 <= pm_sensor_reading <= 35.4:
        aqi = map_range(int(pm_sensor_reading), 12, 35, 51, 100)
        aqi_category = "Moderate"
    elif 35.5 <= pm_sensor_reading <= 55.4:
        aqi = map_range(int(pm_sensor_reading), 36, 55, 101, 150)
        aqi_category = "Unhealthy for Sensitive Groups"
    elif 55.5 <= pm_sensor_reading <= 150.4:
        aqi = map_range(int(pm_sensor_reading), 56, 150, 151, 200)
        aqi_category = "Unhealthy"
    elif 150.5 <= pm_sensor_reading <= 250.4:
        aqi = map_range(int(pm_sensor_reading), 151, 250, 201, 300)
        aqi_category = "Very Unhealthy"
    elif 250.5 <= pm_sensor_reading <= 350.4:
        aqi = map_range(int(pm_sensor_reading), 251, 350, 301, 400)
        aqi_category = "Hazardous"
    elif 350.5 <= pm_sensor_reading <= 500.4:
        aqi = map_range(int(pm_sensor_reading), 351, 500, 401, 500)
        aqi_category = "Hazardous"
    else:
        print("Invalid PM2.5 concentration")
        aqi = -1
        aqi_category = None
    return aqi, aqi_category

def sample_aq_sensor():
    """Samples PM2.5 sensor
    over a 2.3 second sample rate.

    """
    aq_reading = 0
    aq_samples = []

    # initial timestamp
    time_start = time.monotonic()
    # sample pm2.5 sensor over 2.3 sec sample rate
    while (time.monotonic() - time_start <= 2.3):
        try:
            aqdata = pm25.read()
            aq_samples.append(aqdata["particles 25um"])
        except RuntimeError:
            print("Unable to read from sensor, retrying...")
            continue
        # pm sensor output rate of 1s
        time.sleep(1)
    # average sample reading / # samples
    for sample in range(len(aq_samples)):
        aq_reading += aq_samples[sample]
    aq_reading = aq_reading / len(aq_samples)
    aq_samples.clear()
    return aq_reading

def read_bme280(is_celsius=False):
    """Returns temperature and humidity
    from BME280 environmental sensor, as a tuple.

    :param bool is_celsius: Returns temperature in degrees celsius
                            if True, otherwise fahrenheit.
    """
    humidity = bme280.humidity
    temperature = bme280.temperature
    if not is_celsius:
        temperature = temperature * 1.8 + 32
    return temperature, humidity


# Create an instance of the Adafruit IO HTTP client
io = IO_HTTP(secrets['aio_user'], secrets['aio_key'], wifi)

# Describes feeds used to hold Adafruit IO data
feed_aqi = io.get_feed("air-quality-sensor.aqi")
feed_aqi_category = io.get_feed("air-quality-sensor.category")
feed_humidity = io.get_feed("air-quality-sensor.humidity")
feed_temperature = io.get_feed("air-quality-sensor.temperature")


# Set up location metadata
# TODO: Use secrets.py instead!
location_metadata = "40.726190, -74.005334, -6"

elapsed_minutes = 0
prv_mins = 0
aqi_readings = 0

while True:
    print("Obtaining time")
    try:
        cur_time = io.receive_time()
        # print(cur_time)
    except (ValueError, RuntimeError) as e:
            print("Failed to get data, retrying\n", e)
            wifi.reset()
            continue

    if cur_time[4] > prv_mins:
        print("%d min elapsed.."%elapsed_minutes)
        # Sample the AQI every minute
        aqi_readings += sample_aq_sensor()
        prv_mins = cur_time[4]
        elapsed_minutes += 1

    if elapsed_minutes >= 10:
        print("Sampling AQI...")
        # Average AQI over 10 individual readings
        aqi_readings /= 10
        aqi, aqi_category = calculate_aqi(aqi_readings)
        print("AQI: %d"%aqi)
        print("Category: %s"%aqi_category)

        # temp and humidity
        print("Sampling environmental sensor...")
        temperature, humidity = read_bme280()
        print("Temperature: %0.1f F" % temperature)
        print("Humidity: %0.1f %%" % humidity)
        # TODO: Publish all values to Adafruit IO

        try:
            io.send_data(feed_aqi["key"], str(aqi))
            io.send_data(feed_aqi_category["key"], aqi_category)
            io.send_data(feed_temperature["key"], str(temperature))
            io.send_data(feed_humidity["key"], str(humidity))
        except (ValueError, RuntimeError) as e:
            print("Failed to get data, retrying\n", e)
            wifi.reset()
            continue


        # Reset timer
        elapsed_minutes = 0
    time.sleep(30)

