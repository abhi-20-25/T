import cv2
import threading
import time
import logging
import numpy as np
from collections import defaultdict

# --- Configuration ---
QPOS_TIME_THRESHOLD_SEC = 5.0
QPOS_DETECTION_THRESHOLD = 3

class DetectionProcessor(threading.Thread):
    def __init__(self, rtsp_url, channel_id, channel_name, tasks, detection_callback):
        super().__init__(name=f"Detection-{channel_name}")
        self.rtsp_url = rtsp_url
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.tasks = tasks
        self.detection_callback = detection_callback
        
        self.is_running = True
        self.last_detection_times = {task['app_name']: 0 for task in self.tasks}
        self.cooldown = 30
        self.gif_duration_seconds = 3
        self.fps = 10
        self.qpos_persistence_tracker = defaultdict(list)
        self.latest_frame = None
        self.lock = threading.Lock()
        self.cached_boxes = {}  # Cache for bounding boxes

    def stop(self):
        self.is_running = False

    def shutdown(self):
        logging.info(f"Shutting down DetectionProcessor for {self.channel_name}")
        self.is_running = False

    def get_frame(self):
        """Get the latest frame for video streaming - zero-lag optimized"""
        with self.lock:
            if self.latest_frame is not None:
                success, jpeg = cv2.imencode('.jpg', self.latest_frame, [cv2.IMWRITE_JPEG_QUALITY, 50])
                return jpeg.tobytes() if success else b''
            else:
                # Return placeholder while connecting
                placeholder = np.full((480, 640, 3), (22, 27, 34), dtype=np.uint8)
                cv2.putText(placeholder, 'Connecting...', (180, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (201, 209, 217), 2)
                _, jpeg = cv2.imencode('.jpg', placeholder, [cv2.IMWRITE_JPEG_QUALITY, 50])
                return jpeg.tobytes()

    def run(self):
        cap = cv2.VideoCapture(self.rtsp_url)
        if not cap.isOpened():
            logging.error(f"Could not open stream for {self.channel_name}: {self.rtsp_url}")
            return

        is_file = any(self.rtsp_url.lower().endswith(ext) for ext in ['.mp4', '.avi', '.mov'])
        
        last_inference_time = 0
        inference_interval = 0.2  # 5 FPS inference for performance
        
        while self.is_running:
            ret, frame = cap.read()
            if not ret:
                if is_file:
                    logging.info(f"Restarting video file for {self.channel_name}...")
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                else:
                    logging.warning(f"Reconnecting to stream {self.channel_name}...")
                    time.sleep(5)
                    cap.release()
                    cap = cv2.VideoCapture(self.rtsp_url)
                    continue

            current_time = time.time()
            
            # Throttled detection - only run inference periodically
            if current_time - last_inference_time >= inference_interval:
                last_inference_time = current_time
                
                for task in self.tasks:
                    app_name = task['app_name']
                    
                    # Run model inference
                    results = task['model'](
                        frame,
                        conf=task['confidence'],
                        classes=task.get('target_class_id'),
                        verbose=False
                    )
                    
                    # Cache boxes and trigger callbacks
                    if results and len(results[0].boxes) > 0:
                        self.cached_boxes[app_name] = results[0].boxes
                        
                        if current_time - self.last_detection_times[app_name] >= self.cooldown:
                            annotated_frame = results[0].plot()
                            self._trigger_detection_callback(
                                app_name, results, annotated_frame, 
                                current_time, task, cap
                            )
                    else:
                        # Clear cached boxes if no detection
                        self.cached_boxes[app_name] = None
            
            # Draw cached boxes on current frame for live feed
            display_frame = self._draw_cached_boxes(frame)
            
            # Store for streaming
            with self.lock:
                self.latest_frame = display_frame.copy()
        
        cap.release()

    def _draw_cached_boxes(self, frame):
        """Draw cached bounding boxes on frame"""
        annotated = frame.copy()
        
        for app_name, boxes in self.cached_boxes.items():
            if boxes is not None and len(boxes) > 0:
                for box in boxes.xyxy.cpu():
                    x1, y1, x2, y2 = map(int, box)
                    cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)
        
        return annotated

    def _trigger_detection_callback(self, app_name, results, annotated_frame, 
                                    current_time, task, cap):
        """Separate method for detection callbacks"""
        self.last_detection_times[app_name] = current_time
        detection_count = len(results[0].boxes)
        
        if app_name == 'QPOS':
            self.qpos_persistence_tracker[self.channel_id] = [ts for ts in self.qpos_persistence_tracker[self.channel_id] if current_time - ts <= QPOS_TIME_THRESHOLD_SEC]
            if results and len(results[0].boxes) > 0:
                self.qpos_persistence_tracker[self.channel_id].append(current_time)
            
            if len(self.qpos_persistence_tracker[self.channel_id]) >= QPOS_DETECTION_THRESHOLD:
                self.qpos_persistence_tracker[self.channel_id] = []
                self.detection_callback(app_name, self.channel_id, [annotated_frame], "QPOS Screen Off detected.", False)
        
        elif task.get('is_gif', False):
            gif_frames = [annotated_frame]
            for _ in range(self.gif_duration_seconds * self.fps - 1):
                ret_gif, frame_gif = cap.read()
                if not ret_gif:
                    break
                # Annotate GIF frames too
                gif_results = task['model'](frame_gif, conf=task['confidence'], 
                                           classes=task.get('target_class_id'), 
                                           verbose=False)
                if gif_results and len(gif_results[0].boxes) > 0:
                    gif_frames.append(gif_results[0].plot())
                else:
                    gif_frames.append(frame_gif.copy())
                time.sleep(1 / self.fps)
            
            self.detection_callback(app_name, self.channel_id, gif_frames,
                                   f"{app_name} detected ({detection_count} objects).", True)
        else:
            self.detection_callback(app_name, self.channel_id, [annotated_frame],
                                   f"{app_name} detected ({detection_count} objects).", False)
