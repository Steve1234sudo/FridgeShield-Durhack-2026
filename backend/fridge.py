
class Fridge:
    current_temperature = None
    expected_temperature = None
    fridge_id = None

    def __init__(self, fridge_id, current_temperature, expected_temperature):
        self.fridge_id = fridge_id
        self.current_temperature = current_temperature
        self.expected_temperature = expected_temperature

    def detect_temperature_anomaly(self):
        pass

    def send_data_to_neighbours(self):
        pass

    def parse_received_data(self):
        pass