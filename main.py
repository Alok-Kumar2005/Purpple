import cv2

points = []

def click_event(event, x, y, flags, params):
    if event == cv2.EVENT_LBUTTONDOWN:
        print(f"[{x}, {y}],")
        points.append([x, y])
        cv2.circle(img, (x, y), 5, (0, 0, 255), -1)
        coord_text = f"({x}, {y})"
        text_position = (x + 10, y - 10) 
        
        cv2.putText(img, coord_text, text_position, cv2.FONT_HERSHEY_SIMPLEX, 
                    0.45, (0, 255, 255), 1, cv2.LINE_AA)
        
        # 3. Draw a connecting line if a previous point exists
        if len(points) > 1:
            cv2.line(img, tuple(points[-2]), tuple(points[-1]), (255, 0, 0), 2)
            
        cv2.imshow("Coordinate Extractor", img)

if __name__ == "__main__":
    img_path = "image.png" 
    img = cv2.imread(img_path)
    
    if img is None:
        print(f"Error: Unable to load image at '{img_path}'. Verify the filename.")
        exit(1)
    
    img = cv2.resize(img, (1920, 1080))
    
    cv2.namedWindow("Coordinate Extractor")
    cv2.setMouseCallback("Coordinate Extractor", click_event)
    
    cv2.imshow("Coordinate Extractor", img)
    cv2.waitKey(0)
    cv2.destroyAllWindows()